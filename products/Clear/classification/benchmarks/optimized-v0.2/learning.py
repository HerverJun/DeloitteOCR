"""Regularized frozen-feature classifiers selected only on the validation split."""
import warnings

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from threadpoolctl import threadpool_limits


def fit_logistic_head(features, targets, train_mask, val_mask, num_classes, control=lambda: None):
    if np.any(train_mask & val_mask):
        raise ValueError("Training and validation masks overlap")
    if set(targets[train_mask]) != set(range(num_classes)) or set(targets[val_mask]) != set(range(num_classes)):
        raise ValueError("训练及验证集必须覆盖全部类别")
    best = None
    history = []
    with threadpool_limits(limits=4), warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        for strength in (0.1, 1.0, 10.0, 100.0):
            control()
            classifier = LogisticRegression(C=strength, solver="lbfgs", max_iter=1500, tol=1e-6, class_weight="balanced")
            classifier.fit(features[train_mask].astype(np.float64), targets[train_mask])
            probability = classifier.predict_proba(features[val_mask])
            loss = float(log_loss(targets[val_mask], probability, labels=list(range(num_classes))))
            history.append({"C": strength, "val_loss": loss, "iterations": int(max(classifier.n_iter_))})
            if best is None or loss < best[0]:
                best = loss, strength, classifier
    classifier = best[2]
    if num_classes == 2:
        # sklearn's binary probability is sigmoid(z), equivalent to softmax([0,z]).
        weight = np.vstack([np.zeros_like(classifier.coef_), classifier.coef_])
        bias = np.array([0.0, classifier.intercept_[0]])
    else:
        weight, bias = classifier.coef_, classifier.intercept_
    return {"weight": weight.astype(np.float32), "bias": bias.astype(np.float32), "selected_c": best[1], "history": history}
