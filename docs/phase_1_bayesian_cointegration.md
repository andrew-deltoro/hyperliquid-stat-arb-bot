# Phase 1 — Bayesian Cointegration: A Study Guide

*Companion notes for the stat-arb bot. Read alongside McElreath,* Statistical Rethinking, *2nd ed.*

---

## 0. The one-sentence summary

We replaced a z-score formula with a **generative model** of how two crypto prices co-move, estimated by Bayesian inference, and validated by model comparison.

Everything else in this document is elaboration.

---

## 1. The problem in plain English

**Statistical arbitrage** says: if two assets are economically linked, their relative price should hover around a stable equilibrium. When it wanders too far, bet on its return.

The naive version of this is what your original bot did:

```python
ratio = price_A / price_B
z = (ratio - mean(ratio)) / std(ratio)
if z > 2: short A, long B
```

This has **three fatal statistical flaws**:

1. **It assumes the ratio is 1:1** (`price_A / price_B`). Real pairs are not 1:1.
2. **It uses sample mean and SD as if they were the truth**, ignoring estimation uncertainty.
3. **It assumes Gaussian tails**, so a "3σ event" appears impossible when it's actually routine.

Our Bayesian model fixes all three.

---

## 2. What cointegration really means

Two time series `log(A_t)` and `log(B_t)` are **cointegrated** if a linear combination of them is *stationary*:

```
spread_t = log(A_t) − α − β · log(B_t)
```

`spread_t` wanders but is pulled back to a mean. If you can find such an (α, β), you can trade the spread.

**Why log prices, not raw prices?** Prices are ratios and grow multiplicatively. Taking logs turns "A is 20% higher" into a stable additive difference. McElreath Ch 5 §5.2 makes this argument for body mass — the same logic applies here.

**Why not just use a fixed β = 1?** Because ETH and BTC don't move 1:1. From our scan: **β = 1.24** for ETH/BTC. If you traded at 1:1, you'd be long 24% more ETH-beta than you thought, and you'd get wiped out on directional moves. β is the **hedge ratio** and must be estimated.

---

## 3. The generative story (McElreath's central move)

McElreath Ch 1 §1.2 and Ch 2 §2.2:

> **Don't compute statistics. Write down the story of how the data arose, then let Bayes do the arithmetic.**

Our story:

> *Two assets are cointegrated. Log-prices satisfy*
> `log(A_t) = α + β · log(B_t) + ε_t`
> *where `ε_t` is noise with fat tails, because crypto spreads are not Gaussian.*

That's the whole model. Everything else is a choice of priors.

---

## 4. Line-by-line: the model definition

```python
alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
beta  = pm.Normal("beta",  mu=1.0, sigma=1.0)
sigma = pm.HalfCauchy("sigma", beta=1.0)
nu    = pm.Exponential("nu", lam=1/10.0)

mu = alpha + beta * log_b
pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)
```

**`alpha ~ Normal(0, 5)`**
The intercept. On log scale, this is meaningless alone; it just rescales. Prior wide enough that it doesn't matter. (McElreath Ch 4 §4.4.1.3 — "very weak prior for intercepts.")

**`beta ~ Normal(1, 1)`**
Centered at 1.0 because most pairs are *roughly* 1:1. Width 1 allows β from 0 to 2 with 95% probability. This is a **regularizing prior** (Ch 6 §6.3) — it gently discourages absurd hedge ratios unless the data insist.

**`sigma ~ HalfCauchy(1)`**
Half-Cauchy because σ must be positive. This is Stan's and McElreath's default (Ch 8 §8.3) — thick-tailed enough to allow large values but skeptical of extremes. This is what **keeps a single flash crash from destroying the spread estimate for weeks**.

**`nu ~ Exponential(1/10)`**
Student-t degrees of freedom. Prior mean 10, but any positive value allowed. **This is the parameter that lets the model *learn* how fat the tails are.** (Ch 11 §11.3.)

**`StudentT(nu, mu, sigma)`**
Not Gaussian! The observed spread has heavy tails. Assuming Gaussian would give us spuriously tight confidence intervals and wrong trade signals.

---

## 5. Why Student-t instead of Gaussian (Ch 11 §11.3)

Recall the table from Ch 11:

| ν | Tail behavior |
|---|---|
| 1 | Cauchy. Infinite mean & variance. Pathological. |
| **2** | **Infinite variance.** |
| 5 | Noticeable fat tails |
| 30 | Nearly Gaussian |
| ∞ | Exactly Gaussian |

**Our ETH/BTC fit gave ν = 1.99.**

That means the ETH/BTC spread has **infinite variance in the strict sense**. In plain English:

- A "2σ event" happens **not 5% of the time, but roughly 15–20%**
- A "3σ event" happens **not 0.3% of the time, but roughly 5–7%**
- Gaussian-based position sizing will get stopped out *constantly*

**This is exactly why your old bot lost money.** It used sample SD as a size input, but the SD is meaningless when ν < 2.

We use Student-t. Not because it's fancy, but because **Gaussian is wrong.**

---

## 6. Model comparison: why ΔWAIC matters (Ch 6)

We fit **two models**:

1. **Cointegration**: `log(A) = α + β·log(B) + ε` — assumes a relationship
2. **Null**: `log(A) = α + ε` — assumes A is independent of B

Then we compute WAIC (Ch 6 §6.4.2) for each. **The difference tells us how much cointegration improves prediction**:

```
ΔWAIC = WAIC_coint − WAIC_null
      = −1719 (for ETH/BTC)
```

**Negative ΔWAIC means cointegration wins.** The bigger the magnitude, the stronger the evidence.

**Important caveat** (Ch 6 §6.5, McElreath's exact words):

> *"Newcomers often ask whether a difference is 'significant.' In general, it is not possible to provide a principled threshold..."*

But ΔWAIC of −1719 is not a marginal call — it's overwhelming. Any threshold would accept this pair.

**We also normalize by sample size**: `score = ΔWAIC / n_obs`. This lets us compare pairs of different history lengths. More negative = stronger cointegration per observation.

---

## 7. How to read the scan table

| Column | What it means | How to use |
|---|---|---|
| **β** | Hedge ratio | Multiply B's position by this |
| **β_sd** | Uncertainty in β | Small relative to β = well-determined |
| **σ** | Spread dispersion (log scale) | Bigger = harder to trade |
| **ν** | Fat-tailedness | **Low ν = size smaller** |
| **ΔWAIC** | Evidence for cointegration | Negative & large = real pair |
| **score** | ΔWAIC / n | Comparable across pairs |
| **z_now** | Current deviation from mean | Live entry signal |
| **conv** | MCMC converged? | Must be True to trust |

---

## 8. The three key lessons from our scan

**Lesson 1: Don't trust priors about pairs — run the model.**

I expected ARB/OP to be a strong pair. It scored dead last. I expected ETH/BTC to be too drifty. It scored #1. **The scan found the truth; my intuition didn't.**

**Lesson 2: Tail fatness varies wildly across pairs.**

ETH/BTC: ν = 1.99 (pathological)
SEI/SUI: ν = 31 (nearly Gaussian)

A single position-sizing rule will fail. **Sizing must depend on ν.**

**Lesson 3: β ≠ 1 for almost every pair.**

ETH/BTC: β = 1.24
ARB/OP: β = 2.07
LINK/PYTH: β = 0.65

The naive "ratio" approach throws away this information. Hedge ratios must be **estimated**, not assumed.

---

## 9. What Phase 2 will do

The signal we compute won't be a raw z-score. It will be a **posterior reversion probability**:

> *Given the current spread deviation, the estimated mean-reversion speed, and the fat tails, what is the probability the spread returns to equilibrium within H hours?*

That requires one more model — the **Ornstein-Uhlenbeck mean-reversion process** — plus simulation from the posterior predictive distribution. That's Phase 2.

---

## 10. If you remember only five things

1. **Cointegration = a linear combination that is stationary.** Trade the deviation, not the trend.
2. **Estimate β; don't assume it's 1.**
3. **Crypto spreads have fat tails; ν ≈ 2 for ETH/BTC.** Use Student-t.
4. **Compare against a null.** Only trade pairs where ΔWAIC is very negative.
5. **Size differently for each pair**, based on both σ and ν.