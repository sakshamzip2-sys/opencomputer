---
name: security-threat-modeling
description: Use when assessing attack surface, applying STRIDE, threat-modeling a feature, or planning security reviews
---

# Security Threat Modeling

## When to use

- New feature crossing a trust boundary (auth, file upload, external API)
- Pre-launch security review
- Investigating a near-miss security report

## Steps

1. **Draw the data-flow diagram.** Boxes = processes. Arrows = data with direction. Cylinders = stores. Trust boundaries = dashed lines crossing arrows.
2. **STRIDE per element:**
   - **S**poofing: can someone impersonate this actor?
   - **T**ampering: can data in transit / at rest be modified?
   - **R**epudiation: can someone deny they did the action?
   - **I**nformation disclosure: can secrets / PII leak?
   - **D**enial of service: can this component be overloaded?
   - **E**levation of privilege: can a low-priv actor become high-priv?
3. **Per finding: rate likelihood × impact.** High×High = block release. Low×Low = backlog. Most things are in the middle.
4. **Mitigations beat detections.** Prefer designs that *cannot* fail rather than designs that *log when they fail*.
5. **Document the residual risk.** Things you accepted, why, and the trigger to revisit.
6. **Re-run on every architectural change.** A new endpoint or storage class = new flows = new threats.

## Notes

- "Trusted user input" is an oxymoron. Validate at every boundary.
- Don't model users as homogeneous; insider threat is a real category.
- Threat modeling is for *prevention*, not paperwork. If the model never produces a code change, it's theatre.
