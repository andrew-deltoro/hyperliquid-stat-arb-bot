# Phase 2 — Mean Reversion: A Study Guide

*Companion notes for the stat-arb bot. Read alongside McElreath,* Statistical Rethinking, *2nd ed.*

---

## 0. The one-sentence summary

Phase 1 told us *whether* two assets are cointegrated. Phase 2 tells us **how fast** the spread reverts, and converts that into a probability of profitable reversion within a specific time horizon.

Cointegration alone is not a trade signal. It's a *fact about the pair*. The trade signal requires **dynamics**.

---

## 1. Why cointegration alone is not enough

Phase 1 gave us this model:

```
log(A_t) = α + β · log(B_t) + ε_t
```

with `ε_t` — the spread — assumed to be stationary.

But "stationary" is a **long-run** property. It means the spread *tends to come back*. It says **nothing** about:

- **How fast** does it come back?
- **What path** does it take?
- **How far** can it wander before reverting?

Two cointegrated pairs can have completely different dynamics. One might snap back in 2 hours; another might take 2 weeks. Trading them identically would be a mistake.

**Phase 2 fills this gap with a dynamical model of the spread.**

---

## 2. The Ornstein-Uhlenbeck process (McElreath Ch 4 §4.4)

The simplest model of mean reversion is the **Ornstein-Uhlenbeck (OU) process**. In discrete time:

```
S_{t+1} = (1 − κ) · S_t + ε_t
      ε_t ~ StudentT(ν, 0, σ)
```

Read it line by line:

**`S_{t+1} = (1 − κ) · S_t + ε_t`**

The next spread value is a weighted average of the current spread and zero, plus noise.

- **If κ = 0** → `S_{t+1} = S_t + ε_t`. Pure random walk. No memory. No reversion.
- **If κ = 1** → `S_{t+1} = 0 + ε_t`. Instant snap-back to the mean.
- **If κ = 0.05** → `S_{t+1} = 0.95 · S_t + ε_t`. 5% of the deviation corrects each bar.

**κ is the mean-reversion speed.** That's the parameter we care about.

**`ε_t ~ StudentT(ν, 0, σ)`**

Fat-tailed noise. Same reasoning as Phase 1 (McElreath Ch 11 §11.3). Crypto spreads have violent shocks.

---

## 3. The half-life: turning κ into a clock

κ is nice mathematically but hard to trade on. **The half-life is the trader's parameter.**

Half-life = the time it takes for a deviation to shrink to half its value (in expectation).

Derivation: after `h` bars, an expected deviation `S_0` becomes `S_0 · (1 − κ)^h`. We want `(1 − κ)^h = 0.5`:

```
h = log(0.5) / log(1 − κ)
```

Examples:

| κ | Half-life | Feeling |
|---:|---:|---|
| 0.5 | 1 bar | Instantly reverts |
| 0.1 | 6.6 bars | Very fast |
| **0.02** | **34 bars** | **Good tradeable speed** |
| 0.01 | 69 bars | Slow |
| 0.005 | 138 bars | Very slow |
| 0.001 | 693 bars | Effectively no reversion |

**Our pairs: κ ≈ 0.015 → half-life ≈ 45 hours.** That's the tradeable sweet spot.

**Too fast** (κ > 0.1): signal disappears before you can execute. Transaction costs eat the edge.

**Too slow** (κ < 0.005): the "reversion" is indistinguishable from drift. Your time-stop fires before reversion.

---

## 4. Fitting the OU model in PyMC

Our model (in `src/hyperliquid_bot/stats/mean_reversion.py`):

```python
with pm.Model() as model:
    kappa = pm.Beta("kappa", alpha=2.0, beta=2.0)
    sigma = pm.HalfCauchy("sigma", beta=1.0)
    nu = pm.Exponential("nu", lam=1/10.0)

    mu = (1 - kappa) * y_lag
    pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=y_next)
```

**`kappa ~ Beta(2, 2)`** — bounded in (0, 1) by construction. The Beta distribution is the natural prior for a probability-like parameter. `Beta(2, 2)` is roughly uniform but slightly favors the middle. Alternative: `Beta(1, 1)` for uniform.

**Why not a Normal prior?** Because κ must be in (0, 1). A Normal would let the sampler wander into impossible values.

**`sigma ~ HalfCauchy(1)`** — same as Phase 1. Regularizing.

**`nu ~ Exponential(1/10)`** — same as Phase 1. Prior mean 10.

**The likelihood: `StudentT(nu, (1-κ)·S_t, σ)`**

The **mean** of the distribution is `(1-κ)·S_t` — the deterministic reversion part. The **noise** is Student-t.

---

## 5. The critical insight: coint ν ≠ OU ν

This is the most surprising thing we discovered, and it matters enormously.

Look at our LINK/PYTH fits:

| Model | ν estimate |
|---|---:|
| Cointegration | **10.1** |
| OU | **3.9** |

**Same pair, same window, 2.5× different tails.**

Why? **They're modeling different residuals.**

- **Cointegration ν** describes the raw spread. The raw spread has an internal mean-reverting structure, which smooths the distribution. It *looks* nearly Gaussian.
- **OU ν** describes the *innovation* after mean reversion is removed. Once you factor out the pull to equilibrium, what remains is the pure shock. That's much more fat-tailed.

**Practical implication:** if you'd assumed the cointegration ν applies to the OU residuals, you'd size 2.5× too large. **The OU ν is the correct tail parameter for sizing.**

This is McElreath's Ch 11 lesson made concrete: **the residuals of a regression are not the residuals of the raw variable.**

---

## 6. Posterior predictive simulation (McElreath Ch 3 §3.3)

To compute P(revert within H hours), we don't use an analytical formula. We **simulate**.

The algorithm:

```
for each of 10,000 posterior draws:
    sample kappa_s, sigma_s, nu_s from the posterior
    simulate a path of length H:
        for t in 1..H:
            S_t = (1 - kappa_s) * S_{t-1} + StudentT(nu_s, 0, sigma_s)
    check: did S ever cross the target?
```

Return the fraction of paths that hit the target. That's `P(revert)`.

**This is exactly McElreath's Fig 3.6 machinery** — combining parameter uncertainty (posterior draws) with outcome uncertainty (Student-t samples) to get a posterior predictive distribution.

**Important:** we're not using a closed-form OU formula. We're simulating from the model. That's why it's called "posterior predictive" — it propagates both kinds of uncertainty.

---

## 7. Position sizing (McElreath Ch 3 §3.2.3)

Our sizing function:

```python
def position_size(p_revert, sigma, nu, base_notional=1000.0):
    edge = max(0.0, p_revert - 0.5)
    tail_discount = tanh(nu / 20.0)
    vol_discount = min(1.0, 0.02 / sigma)
    return base_notional * edge * tail_discount * vol_discount
```

Three factors:

**`edge = p_revert − 0.5`**
The extra probability above a coin flip. Zero if P ≤ 0.5.

**`tail_discount = tanh(ν/20)`**
How much to discount for fat tails. Bounded in (0, 1):
- ν = 2 → tanh(0.1) ≈ 0.10 → deploy 10% of base
- ν = 10 → tanh(0.5) ≈ 0.46 → deploy 46%
- ν = 30 → tanh(1.5) ≈ 0.91 → deploy 91%
- ν = 100 → ~100%

**This is the "fat tails mean smaller position" rule made quantitative.**

**`vol_discount = min(1, 0.02 / σ)`**
Discount for high spread volatility. If σ > 0.02, deploy less.

**This is a Bayesian decision rule** in the McElreath sense (Ch 3 §3.2.3). We implicitly define a loss function: "bigger bets on shakier pairs = bigger loss." The formula is one way to encode that.

---

## 8. What "P(revert) = 0.90" actually means

**Critical caveat.** The 0.90 is a **conditional** probability. It says:

> *Given that the OU model is the true data-generating process, there is a 90% probability that the spread reverts to half its current deviation within H hours.*

**It does not say** the trade has a 90% probability of profit. It says the *spread reversion* has a 90% probability, *under the model's assumptions*.

**The two ways the 0.90 can be wrong:**

1. **Model misspecification.** The spread might not be OU. It might be trending. It might have regime shifts. If it does, the OU model was wrong and no amount of sampling will save you.
2. **Structural break.** The pair might decouple. If PYTH gets a major listing, or LINK gets hacked, the historical mean is meaningless.

**This is McElreath Ch 2 §2.2:** "Bayesian inference is optimal in the small world. Large-world performance has to be demonstrated, not logically deduced."

**The fix is discipline, not better math:**
- **Time stop:** exit if reversion hasn't happened by 2× the half-life
- **Divergence stop:** exit if the spread widens past 3× entry
- **Re-fit the model regularly.** If κ, β, or ν shifts dramatically, the pair is broken

---

## 9. The full trade, walked through

For LINK/PYTH on 2026-09-13:

```
entry_spread    = −0.0903 (LINK cheap relative to PYTH)
half_life       = 51.0 hours
P(revert)       = 0.906 at horizon 102h
sigma           = 0.0055
nu              = 3.9
```

Position sizing:

```
edge          = 0.906 − 0.5 = 0.406
tail_discount = tanh(3.9/20) ≈ 0.193
vol_discount  = min(1, 0.02/0.0055) = 1.0
notional      = 1000 × 0.406 × 0.193 × 1.0 = $78.4
```

Trade:

```
Direction:      LONG LINK, SHORT PYTH
Hedge ratio:    0.636 (long 1 LINK per 0.636 PYTH shorted)
Target spread:  −0.045 (half the entry deviation)
Stop spread:    −0.271 (3× the entry deviation)
Time stop:      +102 hours (2× half-life)
```

Every parameter is a **posterior estimate**. Every exit rule is a **probability-driven decision**.

---

## 10. If you remember only five things

1. **Cointegration ≠ tradeable.** You need a dynamics model. OU is the simplest one.
2. **κ is the speed; half-life is the clock.** Size time stops and exit horizons to the half-life, not to a fixed 24 hours.
3. **Cointegration ν ≠ OU ν.** The residuals of the raw variable are different from the residuals of the model. **Always use the OU ν for sizing.**
4. **P(revert) = 0.90 is conditional on the model.** Large-world reality can disagree. Time stops and divergence stops are your large-world protection.
5. **Position sizing is a decision rule, not a formula.** The formula encodes a loss function: fat tails and high vol mean smaller bets. Make this explicit or you'll be inconsistent.