---
marp: true
theme: gaia
paginate: true
size: 16:9
---

<!-- _class: lead invert -->

# IntelliSource AI
### "To derive Intelligence from the Source Code"

Enterprise Codebase Intelligence & Automated Architectural Discovery

---

## What Is It?

- An **Enterprise Codebase Intelligence** platform — a building block for autonomous software engineering
- Reverse-engineers unfamiliar repos into human- **and** machine-readable documentation, in minutes
- Structural engine: **Java/Spring Boot today** — Python, TypeScript, JavaScript on the roadmap
- A ready-made **context layer for AI coding agents**, built for engineering leaders, consulting firms, and AI automation platforms

---

## The Problem: Codebase Blindness

- **Key-person dependency** — knowledge trapped with a few engineers; it leaves when they do
- **The onboarding tax** — new hires burn days skim-reading files just to find basic connections
- **Hidden architectural risk** — brittle, high-churn "hotspots" stay invisible until they break production
- **The AI token bill trap** — dumping raw repos into an LLM is slow, expensive, and produces hallucinated structure leadership can't trust

---

## The Solution: Structure-First, AI-Second

**Technical Engine** (deterministic, free)
Static parsing → REST endpoints, complexity, security findings, dependency graph, git-churn — zero API cost, zero hallucination

**AI Engine** (contextual, cheap)
LLM called **only** for semantic synthesis — turning code into plain business meaning — on condensed structure, never raw source, with aggressive caching so no class is ever explained twice

---

## Verified Cost Efficiency — Real Run, Real Numbers

**apolloconfig/apollo** — a real production Java codebase

| Metric | Value |
|---|---|
| Files / Classes analyzed | 200 files / 201 classes |
| Lines of code | 22,319 |
| Served from cache | 169 / 201 classes |
| Total cost | **~$0.10** |
| Token reduction | **71.7%** |
| Security findings | 15 High / 0 Medium / 1 Low |

Not a projection — this is one reproducible run.

---

## Quantified ROI vs. Manual Review

Every run converts its LOC count into a manual-review comparison, at a **disclosed, configurable rate** (default: 200 LOC/hour @ $75/hour):

- **111.6 analyst-hours** saved
- **~$8,370** saved vs. manual review at that pace

A number engineering leadership can cite directly — not an estimate buried in a slide.

---

## De-Risked Change Management

- **Dependency-aware risk mapping** — ranks classes by "fan-in" to surface the highest blast-radius components before you touch them
- **Churn-weighted hotspots** — combines commit frequency with complexity to flag code that's unstable *and* hard to change safely
- **Guaranteed data integrity** — architecture, complexity, and dependencies are all *parsed*, never guessed by an LLM
- Any class the model can't describe is marked `"unavailable"` — never silently fabricated

---

## How the Token Reduction Works

Raw `.java` source → AST parse → strip bodies, imports, boilerplate → keep **signatures + Javadoc + annotations + complexity flags** → that's what the LLM sees

- The model only needs to know what a class *means* — not its implementation
- Everything free (complexity, security, dependencies, churn) is computed by code, not tokens
- Result on a real sample: **244K raw tokens → 69K sent = 71.7% reduction** — measured with the real Anthropic tokenizer, not a character-count guess

---

## Three Synchronized Deliverables, Every Run

- **`report.html`** — human dashboard: project overview, module directory, severity-ranked security alerts, churn-weighted hotspots, dependency fan-in ranking, live cost/ROI tiles
- **`analysis.json`** — same data, machine-readable, ready to plug into workflows
- **`analysis.schema.json`** — the formal JSON Schema — machine-**validatable**, not just machine-readable

---

## Continuous Integration, Not a Snapshot

- Ships as a reusable **GitHub Action** — a ~6-line addition to any repo's CI
- Runs automatically on every pull request
- Cache persists across runs → **zero added cost for unchanged code**
- The architectural map stays continuously refreshed, with zero manual re-runs

---

<!-- _class: lead invert -->

## What's Next: The "Brain" for AI Agents

`analysis.json` = a ready-made **map of the world** for any autonomous coding agent

- **IntelliSource AI** = the Brain (context & knowledge layer, refreshed every PR)
- **Codev** = the Hands (execution & orchestration)
- Agents query the graph — controllers, services, endpoints, dependencies — and check blast radius **before** changing code, not after

**github.com/rajamohamedml/intellisource-ai**
