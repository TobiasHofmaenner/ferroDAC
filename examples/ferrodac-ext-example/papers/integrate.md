# Window integral — turning a spectrum into a scalar

## What it computes

Given a measured 1-D spectrum (e.g. an RGA mass spectrum, intensity vs. m/z), the
**window integral** reports the area under the curve over a fixed range `[lo, hi]`:

$$ A = \int_{lo}^{hi} y(x)\,dx \;\approx\; \sum_i \tfrac{1}{2}\,(y_i + y_{i+1})(x_{i+1} - x_i) $$

i.e. the composite **trapezoidal rule** over the samples that fall inside the window.

## Why it's useful

For a well-separated peak, the integrated area is proportional to the amount of the
species producing it — a cheap, robust **partial-pressure proxy** that needs no fitting
or cracking-pattern library. Route the resulting scalar onto a Bars or Chart panel to
track it over time. For overlapping peaks, prefer a deconvolution processor instead.

## Assumptions & caveats

- The window must enclose the peak and exclude neighbours (no overlap correction).
- Baseline is **not** subtracted — offset a noisy baseline beforehand if it matters.
- Requires ≥ 2 samples inside `[lo, hi]`; otherwise the area is reported as 0.

## References

- Press, W. H. et al. *Numerical Recipes*, §4.1 (Classical Formulas for Integration).
- Trapezoidal rule: any introductory numerical-analysis text.
