# Architecture decision records

One file per decision, numbered in order: `0001-short-title.md`. Keep each one short enough to read on a phone.

Before you pick a number, check main and every open PR so two ADRs never share one, and put the number in the PR title. When several ADRs are started at the same time, the coordinator hands out the numbers.

## Template

```markdown
# 0000: Title of the decision

Status: proposed | accepted (date) | replaced by 00xx
Requirements: IDs this decision affects

## Context
What problem we are solving and what constrains us.

## Options
Each option in a few lines, with its main pro and con.

## Decision
What we chose and why.

## Consequences
What gets easier, what gets harder, what we must watch.
```

An ADR becomes accepted when Arita merges the PR that adds it.
