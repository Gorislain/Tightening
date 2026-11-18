#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ADVANCED AUTOREGRESSION FROM SCRATCH (Python standard library ONLY).

Nonlinear seasonal autoregression with:
  - lagged values, nonlinear interactions, local statistics, Fourier features;
  - selection of differencing / AR order / regularization / harmonics;
  - robust Huber regression (IRLS) + ridge, time-decay sample weights;
  - soft mixture of quiet/volatile regimes (final fitted model);
  - expanding-window, multi-step recursive time-series validation;
  - recursive multistep predictions + moving-block residual bootstrap intervals;
  - optional walk-forward testing and CSV input/output.

Run:
    python advanced_autoregression.py
    python advanced_autoregression.py --csv traffic.csv --column speed \
        --periods 288,2016 --horizon 12 --output forecast.csv

No numpy, pandas, scipy, sklearn, statsmodels, or other dependencies.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


EPS = 1e-10


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _sd(xs):
    if not xs:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _quantile(xs, prob):
    a = sorted(xs)
    if not a:
        raise ValueError('Cannot compute quantile for an empty sample')
    r = min(len(a) - 1, max(0.0, prob * (len(a) - 1)))
    i = int(r)
    j = min(i + 1, len(a) - 1)
    return a[i] * (1.0 - (r - i)) + a[j] * (r - i)


def _sigmoid(x):
    if x >= 0:
        e = math.exp(-min(x, 50.0))
        return 1.0 / (1.0 + e)
    e = math.exp(max(x, -50.0))
    return e / (1.0 + e)


def _cholesky_solve(A, b):
    """Solve SPD matrix system with Cholesky, no linear algebra packages."""
    n = len(b)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = A[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                if s <= 0.0:
                    raise ArithmeticError('Non positive definite system')
                L[i][j] = math.sqrt(s)
            else:
                L[i][j] = s / L[j][j]
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return x


def _weighted_ridge(X, y, weights, ridge):
    """Weighted least-squares with L2 penalty, no intercept penalty."""
    q = len(X[0]) + 1
    gram = [[0.0] * q for _ in range(q)]
    rhs = [0.0] * q
    for x, target, w in zip(X, y, weights):
        if w <= 0:
            continue
        row = [1.0] + x
        for j in range(q):
            v = w * row[j]
            rhs[j] += v * target
            for k in range(j, q):
                gram[j][k] += v * row[k]
    for j in range(q):
        for k in range(j):
            gram[j][k] = gram[k][j]
        gram[j][j] += (1e-7 if j == 0 else ridge)
    return _cholesky_solve(gram, rhs)


class RobustRidge:
    """Feature standardization + iteratively reweighted Huber/ridge regression."""

    def __init__(self, ridge=1.0, huber=1.5, iterations=4):
        self.ridge = ridge
        self.huber = huber
        self.iterations = iterations

    def fit(self, X, y, base_weights):
        if not X or not X[0] or len(X) != len(y):
            raise ValueError('Invalid regression matrix')
        p = len(X[0])
        self.mu = [_mean([r[j] for r in X]) for j in range(p)]
        self.scale = [max(_sd([r[j] for r in X]), 1e-5) for j in range(p)]
        Z = [[(r[j] - self.mu[j]) / self.scale[j] for j in range(p)] for r in X]
        weights = list(base_weights)
        for _ in range(self.iterations):
            self.coef = _weighted_ridge(Z, y, weights, self.ridge)
            residuals = [target - self._predict_normalized(z)
                         for z, target in zip(Z, y)]
            # Robust residual scale, using the median absolute deviation.
            center = statistics.median(residuals)
            mad = statistics.median(abs(r - center) for r in residuals)
            scale = max(1.4826 * mad, 0.03)
            threshold = self.huber * scale
            weights = [base * min(1.0, threshold / (abs(r) + EPS))
                       for base, r in zip(base_weights, residuals)]
        # Final weighted solve with the last IRLS weights.
        self.coef = _weighted_ridge(Z, y, weights, self.ridge)
        return self

    def _predict_normalized(self, z):
        return self.coef[0] + sum(c * v for c, v in zip(self.coef[1:], z))

    def predict(self, row):
        z = [(v - m) / s for v, m, s in zip(row, self.mu, self.scale)]
        return self._predict_normalized(z)


@dataclass(frozen=True)
class Config:
    lags: int
    difference: int
    harmonics: int
    ridge: float


class AdvancedAutoRegressor:
    """An autoregressive forecaster implemented only with the stdlib."""

    def __init__(self, periods=(24,), seed=42, use_regimes=True):
        if any(int(p) != p or p < 2 for p in periods):
            raise ValueError('Periods must be integer >= 2')
        self.periods = tuple(int(p) for p in periods)
        self.rng = random.Random(seed)
        self.use_regimes = use_regimes
        self.fitted = False

    @staticmethod
    def _transform(y, difference):
        if difference == 0:
            return list(y)
        return [y[i] - y[i - 1] for i in range(1, len(y))]

    def _features(self, z, at, config, origin_d, zmean, zscale, anchor):
        """Features at index `at`, depending ONLY on values preceding `at`."""
        p = config.lags
        lags = [max(-8., min(8., (z[at - k] - zmean) / zscale))
                for k in range(1, p + 1)]
        a, b = lags[:min(3, p)], lags[:min(7, p)]
        m3, m7 = _mean(a), _mean(b)
        s3, s7 = _sd(a), _sd(b)
        l1 = lags[0]
        l2 = lags[1] if p > 1 else l1
        v = lags + [
            m3, m7, s3, s7,
            l1 - l2, l1 - m7, max(a) - min(a),
            l1 * l1, l2 * l2, l1 * l2,
            l1 * m3, abs(l1),
            math.tanh(l1), math.tanh(l1 - l2),
            min(at / max(1, anchor), 3.0),
        ]
        absolute_index = at + origin_d
        for period in self.periods:
            for h in range(1, config.harmonics + 1):
                angle = 2.0 * math.pi * h * (absolute_index % period) / period
                v.extend((math.sin(angle), math.cos(angle)))
        # Local volatility for regime weights (uses only past observations).
        volatility = _sd(lags[:min(p, 7)])
        return v, volatility

    def _fit_config(self, values, config, regimes=False):
        z = self._transform(values, config.difference)
        n = len(z)
        if n - config.lags < max(24, config.lags + 5):
            raise ValueError('Insufficient observations for selected AR order')
        zmean = _mean(z)
        zscale = max(_sd(z), 1e-7)
        X, targets, volatility = [], [], []
        for at in range(config.lags, n):
            row, vol = self._features(z, at, config, config.difference,
                                      zmean, zscale, n)
            X.append(row)
            targets.append((z[at] - zmean) / zscale)
            volatility.append(vol)
        nrows = len(X)
        # Recent observations receive more weight; half-life in samples.
        half_life = max(40.0, 0.7 * nrows)
        base = [math.exp(-math.log(2) * (nrows - 1 - i) / half_life)
                for i in range(nrows)]
        model = RobustRidge(ridge=config.ridge).fit(X, targets, base)
        center = statistics.median(volatility)
        spread = max(statistics.median(abs(v - center) for v in volatility) * 1.4826, 0.08)
        calm = storm = None
        # Only fit gated experts on full training, not during hyperparameter CV.
        if regimes and nrows >= 6 * len(X[0]):
            gates = [_sigmoid((v - center) / (1.5 * spread)) for v in volatility]
            calm_w = [w * (1.0 - g) for w, g in zip(base, gates)]
            storm_w = [w * g for w, g in zip(base, gates)]
            calm = RobustRidge(ridge=config.ridge * 2.0).fit(X, targets, calm_w)
            storm = RobustRidge(ridge=config.ridge * 2.0).fit(X, targets, storm_w)
        fitted = dict(config=config, zmean=zmean, zscale=zscale, anchor=n,
                      global_model=model, calm=calm, storm=storm,
                      gate_center=center, gate_spread=spread)
        # In-sample one-step innovations; enough for residual bootstrap.
        errors = []
        for row, v, target in zip(X, volatility, targets):
            predicted = self._predict_scaled(fitted, row, v)
            errors.append((target - predicted) * zscale)
        fitted['errors'] = errors
        return fitted

    @staticmethod
    def _predict_scaled(fitted, row, volatility):
        global_prediction = fitted['global_model'].predict(row)
        if fitted['calm'] is None or fitted['storm'] is None:
            return global_prediction
        g = _sigmoid((volatility - fitted['gate_center']) /
                     (1.5 * fitted['gate_spread']))
        expert_prediction = ((1.0 - g) * fitted['calm'].predict(row)
                             + g * fitted['storm'].predict(row))
        # Global expert stabilizes volatile-regime extrapolation.
        return 0.4 * global_prediction + 0.6 * expert_prediction

    def _next_z(self, z, fitted):
        config = fitted['config']
        row, volatility = self._features(
            z, len(z), config, config.difference, fitted['zmean'],
            fitted['zscale'], fitted['anchor'])
        pred = self._predict_scaled(fitted, row, volatility)
        return fitted['zmean'] + fitted['zscale'] * pred

    def _recursive(self, history, fitted, horizon, noise=None):
        config = fitted['config']
        z = self._transform(history, config.difference)
        last_y = history[-1]
        result = []
        for step in range(horizon):
            next_z = self._next_z(z, fitted)
            if noise is not None:
                next_z += noise[step]
            z.append(next_z)
            last_y = last_y + next_z if config.difference else next_z
            result.append(last_y)
        return result

    def _candidates(self, n, ridge_grid):
        p_values = (3, 6, 12, 24)
        harmonic_values = (0, 1) if self.periods else (0,)
        candidates = []
        for d in (0, 1):
            for p in p_values:
                if n - d - p < max(30, 2 * p):
                    continue
                for harmonic in harmonic_values:
                    if harmonic and min(self.periods) < 2 * harmonic + 1:
                        continue
                    for ridge in ridge_grid:
                        candidates.append(Config(p, d, harmonic, float(ridge)))
        if not candidates:
            raise ValueError('Need more observations (at least about 55)')
        return candidates

    def fit(self, values, validation_horizon=8, folds=2, ridge_grid=(1.5,),
            verbose=True):
        y = [float(v) for v in values]
        if len(y) < 55 or not all(math.isfinite(v) for v in y):
            raise ValueError('Need >=55 finite observations (no NaN/inf)')
        if not ridge_grid or any(r <= 0 for r in ridge_grid):
            raise ValueError('ridge_grid must contain positive values')
        n = len(y)
        horizon = min(max(1, int(validation_horizon)), max(1, n // 12))
        folds = max(1, min(int(folds), 5))
        origins = [n - horizon * j for j in range(folds, 0, -1)]
        candidates = self._candidates(origins[0], ridge_grid)
        sigma = max(_sd(y), 1e-7)
        leaderboard = []
        for config in candidates:
            errors = []
            for origin in origins:
                try:
                    fitted = self._fit_config(y[:origin], config, regimes=False)
                    forecasts = self._recursive(y[:origin], fitted, horizon)
                except (ValueError, ArithmeticError, OverflowError, ZeroDivisionError):
                    errors = []
                    break
                actual = y[origin:origin + horizon]
                errors.extend(pred - truth for pred, truth in zip(forecasts, actual))
            if errors:
                mae = _mean([abs(e) for e in errors])
                rmse = math.sqrt(_mean([e * e for e in errors]))
                # Small complexity penalty discourages spuriously large AR orders.
                complexity = (config.lags + 2 * config.harmonics * len(self.periods)) / n
                score = 0.7 * mae + 0.3 * rmse + 0.02 * sigma * complexity
                leaderboard.append((score, config, mae, rmse))
        if not leaderboard:
            raise RuntimeError('No candidate converged')
        leaderboard.sort(key=lambda x: x[0])
        self.leaderboard_ = leaderboard
        self.best_config_ = leaderboard[0][1]
        self.fitted_model_ = self._fit_config(y, self.best_config_,
                                               regimes=self.use_regimes)
        self.history_ = y
        self.fitted = True
        if verbose:
            print('Selected model:', self.best_config_)
            print('CV: MAE=%.4f RMSE=%.4f (%d folds × %d steps)' %
                  (leaderboard[0][2], leaderboard[0][3], folds, horizon))
            print('Regime experts:', 'enabled' if self.fitted_model_['calm'] else 'not enabled')
        return self

    def predict(self, horizon, confidence=0.90, simulations=500, block=5):
        """Return [(step, prediction, lower, upper), ...].

        Intervals: empirical residual moving-block bootstrap. They are NOT
        formal calibrated coverage guarantees or Bayesian credible intervals.
        """
        if not self.fitted:
            raise RuntimeError('Call fit() first')
        horizon = int(horizon)
        if horizon < 1:
            raise ValueError('horizon >= 1 required')
        if not 0 < confidence < 1:
            raise ValueError('confidence must be between 0 and 1')
        median_path = self._recursive(self.history_, self.fitted_model_, horizon)
        residuals = self.fitted_model_['errors']
        # Use recent residuals, and center them to reduce bias.
        residuals = residuals[-max(80, min(350, len(residuals))):]
        bias = _mean(residuals)
        residuals = [r - bias for r in residuals]
        size = max(1, min(int(block), len(residuals)))
        paths = [[] for _ in range(horizon)]
        for _ in range(max(10, int(simulations))):
            shocks = []
            while len(shocks) < horizon:
                start = self.rng.randrange(len(residuals) - size + 1)
                shocks.extend(residuals[start:start + size])
            path = self._recursive(self.history_, self.fitted_model_, horizon,
                                   noise=shocks[:horizon])
            for i, value in enumerate(path):
                paths[i].append(value)
        tail = (1.0 - confidence) / 2.0
        return [(i + 1, pred, _quantile(paths[i], tail),
                 _quantile(paths[i], 1.0 - tail))
                for i, pred in enumerate(median_path)]

    def backtest(self, values=None, horizon=8, evaluations=6):
        """Strict expanding-window evaluation with selected fixed parameters.

        NOTE: If the same data were used for parameter selection, this test is
        descriptive, not an independent unbiased out-of-sample evaluation.
        For an unbiased test, hold out a final unseen segment before fit().
        """
        if not self.fitted:
            raise RuntimeError('Call fit() first')
        y = self.history_ if values is None else list(map(float, values))
        horizon = int(horizon)
        evaluations = int(evaluations)
        if horizon < 1 or evaluations < 1:
            raise ValueError('horizon and evaluations must be >=1')
        origins = [len(y) - j * horizon for j in range(evaluations, 0, -1)]
        errors, actuals = [], []
        for origin in origins:
            if origin < max(55, 3 * self.best_config_.lags):
                continue
            fitted = self._fit_config(y[:origin], self.best_config_,
                                      regimes=self.use_regimes)
            pred = self._recursive(y[:origin], fitted, horizon)
            truth = y[origin:origin + horizon]
            errors.extend([p - a for p, a in zip(pred, truth)])
            actuals.extend(truth)
        if not errors:
            raise ValueError('Not enough data for backtest')
        mae = _mean([abs(e) for e in errors])
        rmse = math.sqrt(_mean([e * e for e in errors]))
        denominator = sum(abs(a) for a in actuals)
        return {'n_predictions': len(errors), 'MAE': mae, 'RMSE': rmse,
                'WAPE_percent': 100.0 * sum(abs(e) for e in errors) /
                max(denominator, EPS)}


def _demo_data(n=400, seed=23):
    """Synthetic series with trend, daily/weekly seasonality, nonlinear AR."""
    rng = random.Random(seed)
    y = [60.0, 60.2]
    for t in range(2, n):
        daily = 4.0 * math.sin(2 * math.pi * t / 24)
        weekly = 1.5 * math.sin(2 * math.pi * t / 168)
        shock = rng.gauss(0, 1.6 if t < n * 0.68 else 2.9)
        value = (0.63 * y[-1] + 0.18 * y[-2] +
                 0.19 * (60 + 0.023 * t + daily + weekly) +
                 0.0008 * (y[-1] - 60) ** 2 + shock)
        y.append(value)
    return y


def _load_csv(path, column):
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or column not in reader.fieldnames:
            raise ValueError('Column %r not found. Columns: %s' %
                             (column, reader.fieldnames))
        data = []
        for row_id, row in enumerate(reader, start=2):
            cell = (row[column] or '').strip()
            if not cell:
                raise ValueError('Missing value on CSV row %d' % row_id)
            data.append(float(cell))
        return data


def main():
    parser = argparse.ArgumentParser(description='Pure Python advanced autoregression')
    parser.add_argument('--csv', help='Optional input CSV; without it use demo data')
    parser.add_argument('--column', default='value', help='Numeric CSV column')
    parser.add_argument('--periods', default='24,168', help='Comma-separated seasonal periods')
    parser.add_argument('--horizon', type=int, default=12)
    parser.add_argument('--simulations', type=int, default=400)
    parser.add_argument('--confidence', type=float, default=0.90)
    parser.add_argument('--output', default='autoregression_forecast.csv')
    args = parser.parse_args()
    periods = tuple(int(x) for x in args.periods.split(',') if x.strip())
    data = _load_csv(args.csv, args.column) if args.csv else _demo_data()
    model = AdvancedAutoRegressor(periods=periods, seed=42)
    model.fit(data, validation_horizon=min(args.horizon, 12), folds=2)
    results = model.predict(args.horizon, confidence=args.confidence,
                            simulations=args.simulations)
    print('\nStep     Prediction       Lower        Upper')
    for step, prediction, lower, upper in results:
        print('%4d   %12.4f %12.4f %12.4f' % (step, prediction, lower, upper))
    print('\nWalk-forward summary:', model.backtest(horizon=min(args.horizon, 8),
                                                      evaluations=5))
    with open(args.output, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['horizon', 'forecast', 'lower', 'upper'])
        writer.writerows(results)
    print('Saved:', args.output)


if __name__ == '__main__':
    main()
