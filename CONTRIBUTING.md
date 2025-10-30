# Contributing to Ren

> **TL;DR**: Talk before you merge. Keep the system prompt sacred. Don't let Claude write marketing copy into the codebase.

## 🧘 The Zen of Not Going Insane

This project has a maintainer (hi, that's Nate) who would like to keep his sanity intact. Here's how we do that:

### 🚫 The Sacred Spaces

Some parts of this codebase are **sacred** and need pre-approval before changes:

1. **The Agent System Prompt** (`agents/agent.md` or wherever it lives)
   - This is the brain of the operation
   - Changes here affect literally everything
   - **Rule**: Propose changes first, merge later
   - **Why**: Because 44 surprise lines of instructions for one tool is... a lot

2. **MCP Server Architecture**
   - Tool definitions, docstrings, request models
   - These already go into the system prompt automatically
   - **Rule**: Don't duplicate what's already there
   - **Why**: Redundancy is bloat's best friend

3. **Root-level Documentation**
   - README.md should be proportional
   - Feature-specific docs go in `docs/` or `notes/`
   - **Rule**: One feature ≠ half the README
   - **Why**: We're building a tool, not writing a novel

### 🌳 The Git Workflow (aka "How to Not Surprise Nate")

```
┌─────────────────────────────────────────────┐
│  1. Create a branch for your feature        │
│  2. Do your thing                           │
│  3. Merge main INTO your branch (not ←)    │
│  4. Open a PR                               │
│  5. Wait for review                         │
│  6. Celebrate when merged! 🎉              │
└─────────────────────────────────────────────┘
```

**What NOT to do**:
- ❌ Merge directly to main
- ❌ Push surprise changes without discussion
- ❌ Assume "it's fine" for system prompt changes

**What TO do**:
- ✅ Create a branch
- ✅ Open a PR
- ✅ Ask questions in Discord/issues
- ✅ Make a proposal document if you're unsure

### 🤖 The "Vibe Coder" Problem

Look, we love Claude. Claude is great. But Claude with no guardrails is like a puppy with a marker:

**Symptoms of Vibe Coding**:
- Marketing copy in technical docs ("revolutionary", "game-changing", etc.)
- Two-paragraph descriptions for simple tools
- Premature optimization
- Documentation that reads like a sales pitch
- Instructions so long they bias the agent toward overusing one tool

**The Cure**:
- Keep it technical
- Keep it minimal
- Keep it honest
- If a tool needs paragraphs of explanation, the parameters probably need work

### 📏 The Proportionality Principle

> If one feature takes up 50% of the instructions, the agent will use it 50% of the time (even when it shouldn't).

Documentation should be proportional to importance:
- Core functionality: More docs
- Nice-to-have features: Less docs
- Experimental stuff: Minimal docs, clear warnings

### 💬 When in Doubt, Ask First

Not sure if your change needs approval? Here's a quick guide:

| Change Type | Approval Needed? |
|-------------|------------------|
| Bug fix in your domain | Probably not |
| New feature in your domain | Maybe - open a PR |
| System prompt modification | **YES** |
| Tool description changes | **YES** |
| Architecture changes | **YES** |
| Documentation restructure | **YES** |
| Typo fixes | Nah, you're good |

**Pro tip**: Make a proposal document and paste it in Discord/issues. Way better than surprise commits.

### 🎯 The Goal

We're building a clean, maintainable, powerful tool. Not a bloated mess that needs archaeological expeditions to understand.

**Good contributions**:
- Solve real problems
- Are well-documented (but not over-documented)
- Follow existing patterns
- Come with tests when appropriate
- Don't surprise the maintainer

**Less good contributions**:
- Add bloat
- Duplicate existing functionality
- Break existing patterns
- Appear on main without discussion
- Make Nate question his life choices

---

## 🤝 The Spirit of This Document

This isn't about being a control freak. It's about:
- Keeping the codebase clean
- Maintaining architectural consistency
- Avoiding the slow death of bloat
- Making sure everyone's on the same page

**Remember**: The maintainer has to live with every line of code that goes in. Help them not go crazy.

---

## ❓ Questions?

Jump in Discord or open an issue. Seriously, asking is always better than guessing.

Thanks for contributing! 🚀
