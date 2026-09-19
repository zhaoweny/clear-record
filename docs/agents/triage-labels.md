# Triage Labels

The skills speak in terms of five canonical triage roles. Those five are the *triage*
subset of this repo's label vocabulary — the ones that say who picks a ticket up next.
This file maps the roles to the label strings on the instance, then records the other
families beside them.

## The triage roles

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an away-from-keyboard (AFK) agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

All five exist on the instance, `ready-for-human` included.

When a skill mentions a role (e.g. "apply the ready-for-agent triage label"), use the corresponding label string from this table.

Edit the right-hand column to match whatever vocabulary you actually use.

## The other label families

The roles above share one un-prefixed namespace; the rest of the vocabulary is prefixed
by family. A ticket normally wears a `lane/…` label, a `type/…` label when its body
names a type, and `from/scratch`; `status/…` appears only where the import had no role
for the ticket's state.

| Family            | Values                                                             | Meaning |
| ----------------- | ------------------------------------------------------------------ | ------- |
| `lane/<slug>`     | one per lane — 22 lanes                                            | The lane (a former feature directory) the ticket belongs to; `lane/<slug>` is how committed prose names it |
| `type/<t>`        | the ticket's `**Type:**` line, plus `type/spec`                     | The ticket's kind; `type/spec` marks a lane's umbrella ticket, whose body is the lane's spec |
| `status/<value>`  | the original `**Status:**` value when it was not one of the five roles — `done`, `resolved` and the like | The imported state, recorded as a label because the roles did not cover it |
| `from/scratch`    | always the same string on every imported ticket                     | Marks everything that came from the frozen archive (ADR-0029), so imported work is distinguishable from work begun here |

A skill's "triage label" always means one of the five roles. `lane/…`, `type/…`,
`status/…` and `from/scratch` are never applied as a role.
