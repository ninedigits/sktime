#!/usr/bin/env python3 -u
# copyright: sktime developers, BSD-3-Clause License (see LICENSE file).
"""
Implements the FallbackForecaster.

The FallbackForecaster works with a list of forecasters and tries to fit them in order.
If the active forecaster fails during prediction, it proceeds to the next. This ensures
a robust forecasting mechanism by providing fallback options.
"""

__author__ = ["ninedigits"]
__all__ = ["FallbackForecaster"]

from sktime.base import _HeterogenousMetaEstimator
from sktime.datatypes import ALL_TIME_SERIES_MTYPES
from sktime.forecasting.base._delegate import _DelegatedForecaster
from sktime.utils.warnings import warn


class FallbackForecaster(_HeterogenousMetaEstimator, _DelegatedForecaster):
    """Forecaster that sequentially tries a list of forecasting models.

    Attempts to fit the provided forecasters in the order they are given. If a
    forecaster fails during fitting or prediction, it proceeds to the next one. This
    class is useful in scenarios where the reliability of individual forecasting models
    may be in question, and a fallback mechanism is desired.

    Parameters
    ----------
    forecasters : list of forecasters, or
        list of tuples (str, estimator) of sktime forecasters
        Forecasters to be tried sequentially.
        These are "blueprint" transformers resp forecasters,
        forecaster states do not change when `fit` is called

    warn : bool, default=False
        If True, raises warnings when a forecaster fails to fit or predict.

    Attributes
    ----------
    forecasters_ : list of (str, estimator) tuples
        The forecasters to be tried sequentially.
        Forecasters that have been fitted successfully are stored in this list.
    first_nonfailing_forecaster_index_ : int
        Index of the first non-failing forecaster in the list of forecasters.
    current_forecaster_ : sktime forecaster
        pointer to the first forecaster that was successfully fitted
        same as ``forecasters_[first_nonfailing_forecaster_index_][1]``
    current_name_ : str
        name of the current forecaster
        same as ``forecasters_[first_nonfailing_forecaster_index_][0]``
    exceptions_raised_ : dict
        dictionary of exceptions raised by forecasters during fitting or prediction
        keys are int indices of forecasters in the list of forecasters
        values are dicts with keys "failed_at_step", "exception", "forecaster_name"
        failed_at_step is either "fit" or "predict"
        exception is the exception raised by the forecaster
        forecaster_name is the name of the forecaster

    Examples
    --------
    >>> from sktime.forecasting.naive import NaiveForecaster
    >>> from sktime.forecasting.compose import FallbackForecaster
    >>> from sktime.forecasting.compose import EnsembleForecaster
    >>> from sktime.forecasting.trend import PolynomialTrendForecaster
    >>> from sktime.datasets import load_airline
    >>> y = load_airline()
    >>> # first fit polyomial trend, if fails make naive forecast
    >>> forecasters = [
    ...     ("poly", PolynomialTrendForecaster()),
    ...     ("naive", NaiveForecaster())
    ... ]
    >>> forecaster = FallbackForecaster(forecasters=forecasters)
    >>> forecaster.fit(y=y, fh=[1, 2, 3])
    FallbackForecaster(...)
    >>> y_pred = forecaster.predict()
    """

    _tags = {
        "authors": ["ninedigits"],
        "maintainers": ["ninedigits"],
        "handles-missing-data": True,
        "scitype:y": "both",
        "y_inner_mtype": ALL_TIME_SERIES_MTYPES,
        "X_inner_mtype": ALL_TIME_SERIES_MTYPES,
        "fit_is_empty": False,
        "capability:pred_int": True,
    }
    _delegate_name = "estimator_"
    # for default get_params/set_params from _HeterogenousMetaEstimator
    # _steps_attr points to the attribute of self
    # which contains the heterogeneous set of estimators
    # this must be an iterable of (name: str, estimator, ...) tuples for the default
    _steps_attr = "_forecasters"
    # if the estimator is fittable, _HeterogenousMetaEstimator also
    # provides an override for get_fitted_params for params from the fitted estimators
    # the fitted estimators should be in a different attribute, _steps_fitted_attr
    # this must be an iterable of (name: str, estimator, ...) tuples for the default
    _steps_fitted_attr = "forecasters_"

    def __init__(self, forecasters, verbose=False):
        super().__init__()

        self.forecasters = forecasters
        self.current_forecaster_ = None
        self.estimator_ = None
        self.current_name_ = None
        self.verbose = verbose

        self._forecasters = self._check_estimators(
            forecasters, "forecasters", clone_ests=False
        )
        self.forecasters_ = self._check_estimators(forecasters, "forecasters")

        self._anytagis_then_set("requires-fh-in-fit", True, False, self._forecasters)

    def _predict_interval(self, fh, X, coverage):
        pred_int_capability = True
        nonvalid_estimators = []
        for ix, (name, forecaster) in enumerate(self.forecasters):
            if not forecaster.get_tag("capability:pred_int"):
                pred_int_capability = False
                nonvalid_estimators.append(
                    {
                        "index": ix,
                        "name": name,
                        "estimator": forecaster.__class__.__name__,
                    }
                )
        if pred_int_capability:
            return self.current_forecaster_.predict_interval(fh, X, coverage)
        else:
            raise AttributeError(
                "All forecasters must have prediction capbility "
                "enabled to call `predict_interval`, but at least one"
                "forecaster is missing this capability, see: "
                f"{nonvalid_estimators}"
            )

    def _fit(self, y, X=None, fh=None):
        """Fit the forecasters in the given order until one succeeds.

        Parameters
        ----------
        y : array-like
            Target time series to which to fit the forecasters.
        X : array-like, optional (default=None)
            Exogenous variables.
        fh : array-like, optional (default=None)
            The forecasting horizon.

        Returns
        -------
        self : an instance of self

        Raises
        ------
        RuntimeError
            If all forecasters fail to fit.
        """
        self.first_nonfailing_forecaster_index_ = 0
        self.exceptions_raised_ = dict()
        return self._try_fit_forecasters(y=y, X=X, fh=fh)

    def _try_fit_forecasters(self, y, X, fh):
        """
        Attempt to fit the forecasters in sequence until one succeeds.

        This method iterates over the forecasters starting from the index
        `first_nonfailing_forecaster_index_`. For each forecaster, it tries to fit it
        with the current data. If the fit method of a forecaster raises an exception,
        it records the exception and proceeds to the next forecaster. If a forecaster
        fits successfully, it updates the current forecaster and its name.

        Returns
        -------
        self : an instance of self

        Raises
        ------
        RuntimeError
            If all forecasters fail to fit.
        """
        while True:
            ix = self.first_nonfailing_forecaster_index_
            if self.first_nonfailing_forecaster_index_ >= len(self.forecasters_):
                raise RuntimeError("No remaining forecasters to attempt prediction.")
            name, forecaster = self.forecasters_[ix]
            try:
                self.current_name_ = name
                self.current_forecaster_ = forecaster.clone()
                self.estimator_ = self.current_forecaster_
                self.current_forecaster_.fit(y=y, X=X, fh=fh)
                return self
            except Exception as e:
                self.exceptions_raised_[self.first_nonfailing_forecaster_index_] = {
                    "failed_at_step": "fit",
                    "exception": e,
                    "forecaster_name": name,
                }
                self.first_nonfailing_forecaster_index_ += 1
                if self.verbose:
                    warn(
                        f"Forecaster {name} failed to fit with error: {e}",
                        stacklevel=2,
                        obj=self,
                    )

    def _predict(self, fh, X=None):
        """Predict using the current forecaster.

        If predict fails, fit and predict with the next forecaster.

        Parameters
        ----------
        fh : array-like
            The forecasting horizon.
        X : array-like, optional (default=None)
            Exogenous variables.

        Returns
        -------
        y_pred : array-like
            The predicted values.

        Raises
        ------
        RuntimeError
            If no forecaster is fitted or all forecasters fail to predict.
        """
        if self.current_forecaster_ is None:
            raise RuntimeError("No forecaster has been successfully fitted yet.")

        try:
            return self.current_forecaster_.predict(fh, X)
        except Exception as e:
            self.exceptions_raised_[self.first_nonfailing_forecaster_index_] = {
                "failed_at_step": "fit",
                "exception": e,
                "forecaster_name": self.current_name_,
            }
            if self.verbose:
                warn(
                    f"Current forecaster failed at prediction with error: {e}",
                    stacklevel=2,
                    obj=self,
                )
            self.first_nonfailing_forecaster_index_ += 1

            # Fit the next forecaster and retry prediction
            self.current_forecaster_ = None
            self._try_fit_forecasters(self._y, self._X, self._fh)
            return self.predict(fh, X)

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.

        Returns
        -------
        params : dict or list of dict
        """
        from sktime.forecasting.compose._reduce import YfromX
        from sktime.forecasting.naive import NaiveForecaster

        # univariate case
        FORECASTER = NaiveForecaster()
        params = [{"forecasters": [("f1", FORECASTER), ("f2", FORECASTER)]}]

        # test multivariate case, i.e., ensembling multiple variables at same time
        FORECASTER2 = YfromX.create_test_instance()
        params = params + [{"forecasters": [FORECASTER2, FORECASTER]}]

        return params
