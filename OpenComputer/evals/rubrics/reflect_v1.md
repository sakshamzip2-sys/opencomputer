# reflect_v1 — rubric for Reflection Insights

Grade the response as **correct** ONLY if it satisfies ALL of:

1. **Identifies a real pattern.** The insight names a specific, concrete pattern in the input events — not a generic platitude ("you should write better code"). It cites at least one specific tool, action, or outcome from the trajectory.

2. **Attributes correctly.** The insight's claimed cause matches the actual structure of the events. It doesn't invent events that aren't in the trajectory.

3. **Suggests an actionable change.** The insight proposes something specific the agent could do differently next time — not just "do better." Action items must be testable: "use TodoWrite before multi-step edits" is actionable; "be more careful" is not.

4. **Avoids obvious genericity.** Reject responses that would apply equally well to any session. The insight must be specific enough that it would NOT make sense applied to a different trajectory.

5. **Is honest about uncertainty.** If the trajectory has only 1-2 events, the response should acknowledge limited signal rather than pattern-match aggressively.

Mark **incorrect** if any criterion fails. When borderline, prefer incorrect — false-positive insights pollute the procedural memory loop downstream.
