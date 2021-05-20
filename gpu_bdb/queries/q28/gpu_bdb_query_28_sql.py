#
# Copyright (c) 2019-2020, NVIDIA CORPORATION.
# Copyright (c) 2019-2020, BlazingSQL, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import sys
import os
import time

from bdb_tools.cluster_startup import attach_to_cluster
from cuml.feature_extraction.text import HashingVectorizer
import cupy
import dask
from distributed import wait
import cupy as cp
import numpy as np

from bdb_tools.utils import (
    benchmark,
    gpubdb_argparser,
    run_query,
)


N_FEATURES = 2 ** 23  # Spark is doing 2^20
ngram_range = (1, 2)
preprocessor = lambda s:s.str.lower()
norm = None
alternate_sign = False


def gpu_hashing_vectorizer(x):
    vec = HashingVectorizer(n_features=N_FEATURES,
                            alternate_sign=alternate_sign,
                            ngram_range=ngram_range,
                            norm=norm,
                            preprocessor=preprocessor
     )
    return vec.fit_transform(x)


def map_labels(ser):
    import cudf
    output_ser = cudf.Series(cudf.core.column.full(size=len(ser), fill_value=2, dtype=np.int32))
    zero_flag = (ser==1) | (ser==2)
    output_ser.loc[zero_flag]=0

    three_flag = (ser==3)
    output_ser.loc[three_flag]=1

    return output_ser


def build_features(t):
    X = t["pr_review_content"]
    X = X.map_partitions(
        gpu_hashing_vectorizer,
        meta=dask.array.from_array(
            cupy.sparse.csr_matrix(cupy.zeros(1, dtype=cp.float32))
        ),
    )

    X = X.astype(np.float32).persist()
    X.compute_chunk_sizes()

    return X


def build_labels(reviews_df):
    y = reviews_df["pr_review_rating"].map_partitions(map_labels)
    y = y.map_partitions(lambda x: cupy.asarray(x, cupy.int32)).persist()
    y.compute_chunk_sizes()

    return y


def categoricalize(num_sr):
    return num_sr.astype("str").str.replace(["0", "1", "2"], ["NEG", "NEUT", "POS"])


def sum_tp_fp(y_y_pred, nclasses):

    y, y_pred = y_y_pred
    res = cp.zeros((nclasses, 2), order="F")

    for i in range(nclasses):
        pos_pred_ix = cp.where(y_pred == i)[0]

        # short circuit
        if len(pos_pred_ix) == 0:
            res[i] = 0
            break

        tp_sum = (y_pred[pos_pred_ix] == y[pos_pred_ix]).sum()
        fp_sum = (y_pred[pos_pred_ix] != y[pos_pred_ix]).sum()
        res[i][0] = tp_sum
        res[i][1] = fp_sum
    return res


def precision_score(client, y, y_pred, average="binary"):
    from cuml.dask.common.input_utils import DistributedDataHandler

    nclasses = len(cp.unique(y.map_blocks(lambda x: cp.unique(x)).compute()))

    if average == "binary" and nclasses > 2:
        raise ValueError

    if nclasses < 2:
        raise ValueError("Single class precision is not yet supported")

    ddh = DistributedDataHandler.create([y, y_pred])

    precision_scores = client.compute(
        [
            client.submit(sum_tp_fp, part, nclasses, workers=[worker])
            for worker, part in ddh.gpu_futures
        ],
        sync=True,
    )

    res = cp.zeros((nclasses, 2), order="F")

    for i in precision_scores:
        res += i

    if average == "binary" or average == "macro":

        prec = cp.zeros(nclasses)
        for i in range(nclasses):
            tp_sum, fp_sum = res[i]
            prec[i] = (tp_sum / (tp_sum + fp_sum)).item()

        if average == "binary":
            return prec[nclasses - 1].item()
        else:
            return prec.mean().item()
    else:
        global_tp = cp.sum(res[:, 0])
        global_fp = cp.sum(res[:, 1])

        return global_tp / (global_tp + global_fp).item()


def local_cm(y_y_pred, unique_labels, sample_weight):

    y_true, y_pred = y_y_pred
    labels = unique_labels

    n_labels = labels.size

    # Assume labels are monotonically increasing for now.

    # intersect y_pred, y_true with labels, eliminate items not in labels
    ind = cp.logical_and(y_pred < n_labels, y_true < n_labels)
    y_pred = y_pred[ind]
    y_true = y_true[ind]

    if sample_weight is None:
        sample_weight = cp.ones(y_true.shape[0], dtype=np.int64)
    else:
        sample_weight = cp.asarray(sample_weight)

    sample_weight = sample_weight[ind]

    cm = cp.sparse.coo_matrix(
        (sample_weight, (y_true, y_pred)), shape=(n_labels, n_labels), dtype=cp.float32,
    ).toarray()

    return cp.nan_to_num(cm)


def confusion_matrix(client, y_true, y_pred, normalize=None, sample_weight=None):
    from cuml.dask.common.input_utils import DistributedDataHandler

    unique_classes = cp.unique(y_true.map_blocks(lambda x: cp.unique(x)).compute())
    nclasses = len(unique_classes)

    ddh = DistributedDataHandler.create([y_true, y_pred])

    cms = client.compute(
        [
            client.submit(
                local_cm, part, unique_classes, sample_weight, workers=[worker]
            )
            for worker, part in ddh.gpu_futures
        ],
        sync=True,
    )

    cm = cp.zeros((nclasses, nclasses))
    for i in cms:
        cm += i

    with np.errstate(all="ignore"):
        if normalize == "true":
            cm = cm / cm.sum(axis=1, keepdims=True)
        elif normalize == "pred":
            cm = cm / cm.sum(axis=0, keepdims=True)
        elif normalize == "all":
            cm = cm / cm.sum()
        cm = cp.nan_to_num(cm)

    return cm


def accuracy_score(client, y, y_hat):
    from uuid import uuid1
    from cuml.dask.common.input_utils import DistributedDataHandler

    ddh = DistributedDataHandler.create([y_hat, y])

    def _count_accurate_predictions(y_hat_y):
        y_hat, y = y_hat_y
        y_hat = cp.asarray(y_hat, dtype=y_hat.dtype)
        y = cp.asarray(y, dtype=y.dtype)
        return y.shape[0] - cp.count_nonzero(y - y_hat)

    key = uuid1()

    futures = client.compute(
        [
            client.submit(
                _count_accurate_predictions,
                worker_future[1],
                workers=[worker_future[0]],
                key="%s-%s" % (key, idx),
            )
            for idx, worker_future in enumerate(ddh.gpu_futures)
        ],
        sync=True,
    )

    return sum(futures) / y.shape[0]


def post_etl_processing(client, train_data, test_data):
    import cudf
    from cuml.dask.naive_bayes import MultinomialNB as DistMNB
    from cuml.dask.common import to_dask_cudf
    from cuml.dask.common.input_utils import DistributedDataHandler

    # Feature engineering
    X_train = build_features(train_data)
    X_test = build_features(test_data)

    y_train = build_labels(train_data)
    y_test = build_labels(test_data)

    # Perform ML
    model = DistMNB(client=client, alpha=0.001)
    model.fit(X_train, y_train)

    ### this regression seems to be coming from here
    test_pred_st = time.time()
    y_hat = model.predict(X_test).persist()

    # Compute distributed performance metrics
    acc = accuracy_score(client, y_test, y_hat)

    print("Accuracy: " + str(acc))
    prec = precision_score(client, y_test, y_hat, average="macro")

    print("Precision: " + str(prec))
    cmat = confusion_matrix(client, y_test, y_hat)

    print("Confusion Matrix: " + str(cmat))
    metric_et = time.time()

    # Place results back in original Dataframe

    ddh = DistributedDataHandler.create(y_hat)
    test_preds = to_dask_cudf(
        [client.submit(cudf.Series, part) for w, part in ddh.gpu_futures]
    )

    test_preds = test_preds.map_partitions(categoricalize)

    test_data["prediction"] = test_preds

    final_data = test_data[["pr_review_sk", "pr_review_rating", "prediction"]].persist()

    final_data = final_data.sort_values("pr_review_sk").reset_index(drop=True)
    wait(final_data)
    return final_data, acc, prec, cmat


def read_tables(data_dir, bc):
    bc.create_table("product_reviews", os.path.join(data_dir, "product_reviews/*.parquet"))


def main(data_dir, client, bc, config):
    benchmark(read_tables, data_dir, bc, dask_profile=config["dask_profile"])

    # 10 % of data
    query1 = """
        SELECT
            pr_review_sk,
            pr_review_rating,
            pr_review_content
        FROM product_reviews
        WHERE mod(pr_review_sk, 10) IN (0)
        AND pr_review_content IS NOT NULL
        ORDER BY pr_review_sk
    """
    test_data = bc.sql(query1)

    # 90 % of data
    query2 = """
        SELECT
            pr_review_sk,
            pr_review_rating,
            pr_review_content
        FROM product_reviews
        WHERE mod(pr_review_sk, 10) IN (1,2,3,4,5,6,7,8,9)
        AND pr_review_content IS NOT NULL
        ORDER BY pr_review_sk
    """
    train_data = bc.sql(query2)
    return train_data

    # final_data, acc, prec, cmat = post_etl_processing(
    #     client=client, train_data=train_data, test_data=test_data
    # )

    # payload = {
    #     "df": final_data,
    #     "acc": acc,
    #     "prec": prec,
    #     "cmat": cmat,
    #     "output_type": "supervised",
    # }

    # return payload


if __name__ == "__main__":
    config = gpubdb_argparser()
    client, bc = attach_to_cluster(config, create_blazing_context=True)
    run_query(config=config, client=client, query_func=main, blazing_context=bc)
