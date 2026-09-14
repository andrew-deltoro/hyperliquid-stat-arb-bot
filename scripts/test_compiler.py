"""Minimal PyMC compile test — confirms NUTS works end-to-end."""
import pymc as pm
import numpy as np

with pm.Model() as m:
    x = pm.Normal("x", mu=0, sigma=1)
    # This triggers compilation of the log-probability function
    samples = pm.draw(x, draws=5, random_seed=0)

print("draw works:", samples)
print()

# Now the real test: NUTS sampling (requires full compile + C extensions)
with pm.Model() as m2:
    x = pm.Normal("x", mu=0, sigma=1)
    idata = pm.sample(
        draws=50, tune=50, chains=2, cores=1,   # <-- add cores=1
        progressbar=False, random_seed=0,
    )

print("NUTS works")
print("posterior mean of x:", float(idata.posterior["x"].mean()))