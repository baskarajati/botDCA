# Training-supported entry uncertainty diagnostic

## Outcome

The predeclared entry-price sensitivity grid produces joint close-time/depth
matches for **4/7** previously failing cycles on both paths. Of those, **3/7**
have a matching seed inside the observed entry-second public price range.
The remaining recovery needs a seed outside that second's range. These are
conditional sensitivity results, not confirmed fills or a better strategy score.

Different cases need positive and negative price shifts. One common normalized
offset recovers at most 2/7. Per-case hindsight selection must not become a
deployable rule or an inferred new 80-cycle score. Matching means the original
300-second close tolerance and observed DCA depth, not identical fill prices,
final averages, quantities, exits, P&L or private order behavior.

In particular, the successful sampled seeds for the two depth-zero recoveries
differ from their known exported initial averages by more than the assumed
0.0005 display half-width. Those are counterfactual threshold sensitivities,
not reconstructed actual entries. The finite grid does not sample every known
average exactly, so this also does not rule out a match at an unsampled price.
Never treat the 4/7 or 3/7 counts as four or three proven explanations.

## Frozen bounds

No private per-order execution export was found in the local data directory.
The approved fallback sampled the last 20 depth-zero training cycles in each
eligible regime, excluding the later diagnostic segment. Their exported final
averages identify initial-position averages without assuming DCA fill prices.
Display uncertainty was assumed to be plus/minus 0.0005 USDT.

| Regime | Training sample | Training dates | Symmetric relative seed-price box |
| --- | ---: | ---: | ---: |
| 3 | 20 | 11 | plus/minus 0.061037% |
| 4 | 20 | 6 | plus/minus 0.037682% |

The boxes are the maximum training-sample discrepancies from the first public
print at/after the exported entry second, including display uncertainty. They
are not confidence intervals, slippage estimates, full-training extrema or
guaranteed out-of-sample bounds. Existing whole-export regime segmentation is
inherited; this is not fresh validation.

All 40 training averages intersect their exported second's public OHLC range.
The nearest price-range occurrence proxy consequently has lag zero, with no
censored training cases inside the plus/minus 60-second search. This supplies
**no evidence for a nonzero timing range**. It does not identify actual fill
timestamps or establish zero latency. No delayed DCA-admission rule was tested.
The timing sensitivity candidates collapse to the baseline; actual execution
timing uncertainty remains unresolved.

## Per-case results

Times below are UTC. Successful seeds need not be individual public prints:
range inclusion is only necessary supporting evidence, not execution proof.

| Opened | Observed depth | Frozen-grid result |
| --- | ---: | --- |
| Aug 13 04:37:09 | 0 | A -0.5-box seed (56.832650) matches the close second and is inside 56.82–56.85. |
| Aug 13 07:09:26 | 1 | +1-box seed (57.435035) matches within 120 seconds, but the exported second contains only price 57.40. Weak, counterfactual recovery. |
| Aug 27 14:38:03 | 0 | +0.5-box seed (84.175857) matches the close second and is inside 84.16–84.23. |
| Aug 27 18:41:03 | 4 | No tested candidate produces a joint match. |
| Aug 29 14:27:25 | 0 | No tested candidate produces a joint match; see the known price-box limitation below. |
| Sep 02 05:25:14 | 3 | No tested candidate produces a joint match. |
| Sep 03 23:42:04 | 4 | -0.5-box seed (87.823450) matches the close second and is inside 87.79–87.85. |

The Aug 29 exported depth-zero average is 82.06 versus first print 82.14,
about -0.0974%, outside regime 4's learned box. Nevertheless, that average is
inside the observed second's 82.04–82.14 range. This is direct evidence that the
small training sample's discrepancy box does not contain every real later seed
error. Do not label this failure a hidden strategy rule or proof that entry
uncertainty cannot explain it. More complete training and private execution
observations would be needed to justify broader empirical bounds.

## Method and evidence

The approved design tested price offsets -1, -0.5, 0, +0.5, +1 times the frozen
regime box. Entry timestamp, median training TP, DCA ladder, normalized quantities
and fees stayed unchanged. Synthetic seeds were inserted into the first candle;
those counterfactual seeds are not guaranteed executable prices. Both paths were
run. No simultaneous price/timing fitting or per-failure TP tuning was done.
The unshifted control reproduced all seven prior failures.

No match on this finite grid does not exclude solutions between grid points,
outside the sample box, or under a different evidenced execution-timing model.
Public range overlap does not establish queue position or private VWAP.

Fifteen missing training-date daily archives were downloaded outside the
repository. The analysis read 25 daily files and 11,981,013 public trades to
EOF, covering 40 short training-entry windows and seven diagnostic cycle windows.
All requested order and close seconds were present; there were no timestamp
ordering defects or retained duplicate IDs. The loader's `entry_second_present`
field refers to the start of the expanded search window (60 seconds before the
exported entry), not actual entry; actual entry is included in requested order
seconds. Archive completeness and private fill reconstruction remain unproven.

Private derived output remains outside the repository at
`Crypto_Quant/data/HYPEUSDT-zuya-training-entry-uncertainty-2026-09-17.json`.
Its public-range annotations were independently checked against raw archive
prints without changing replay outcomes.

## Recommendation

Entry-price uncertainty is material, but it cannot identify a universal entry
rule from this CSV. The strongest unresolved structural candidates are the
Aug 27 deep cycle and Sep 2's unobserved DCA4. Obtain individual execution prices,
timestamps and order IDs before choosing among delayed admission, alternative
trigger construction and different seed histories. Until then, preserve the
unknowns rather than adding an entry filter or DCA9–10 behavior to fit them.

The brainstorming/debugging skills kept sampling, bounds and candidate grid
predeclared and training-only. Verification checked sampling leakage, display
intervals, censoring and missing-data failure behavior. No runtime change,
commit, push or PR update was made.
