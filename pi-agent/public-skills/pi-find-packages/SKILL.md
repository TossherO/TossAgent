---
name: pi-find-packages
description: Use when users want to find, compare, recommend, install, or learn about Pi packages from https://pi.dev/packages or npm packages tagged pi-package.
---

# Pi Find Packages

This skill helps discover installable Pi packages from the Pi package gallery ecosystem. Pi packages are npm packages tagged `pi-package` and can bundle extensions, skills, prompts, and themes.

## When to Use

Use this skill when the user:

- Asks to find a Pi package for a capability, workflow, extension, skill, prompt, or theme
- Mentions https://pi.dev/packages or wants to browse Pi packages
- Asks whether a Pi package exists for a feature such as subagents, MCP, web access, todo lists, themes, or prompts
- Wants an install command for a Pi package
- Wants quality/relevance checks before installing a third-party Pi package

## Package Search CLI

Use the installed package binary when available:

```bash
pi-find-packages [query...] --limit 10
pi-find-packages [query...] --limit 10 --json
```

When developing inside this package directory, use the script directly:

```bash
node scripts/search-pi-packages.mjs [query...] --limit 10 --json
```

The CLI wraps the npm registry search API with `keywords:pi-package`, matching packages shown by the Pi package gallery.

Examples:

```bash
pi-find-packages subagents --limit 5
pi-find-packages mcp adapter --json
pi-find-packages theme --limit 20
```

## Workflow

### 1. Understand the Need

Identify:

1. Desired capability or domain
2. Whether the user wants discovery only, a recommendation, or installation
3. Any constraints: official packages only, local install (`-l`), pinned version, no extensions, etc.

### 2. Search Packages

Use the wrapper first:

```bash
pi-find-packages <keywords> --limit 10 --json
```

If the binary is not on PATH and you are in this package directory, run:

```bash
node scripts/search-pi-packages.mjs <keywords> --limit 10 --json
```

If results are weak, try synonyms:

| Need | Queries to try |
| --- | --- |
| Subagents | `subagents`, `agents`, `parallel`, `delegation` |
| MCP | `mcp`, `model context protocol`, `adapter` |
| Web access | `web`, `search`, `fetch`, `scrape`, `browser` |
| Planning/todos | `todo`, `plan`, `workflow` |
| UI/theme | `theme`, `tui`, `ui` |

### 3. Verify Before Recommending

Do not recommend based only on a package name. Check likely candidates:

```bash
npm view <package-name> name version description keywords repository homepage license time --json
npm view <package-name> pi --json
```

Prefer packages that:

- Include the `pi-package` keyword
- Have a relevant description and recent version
- Expose a `pi` manifest or conventional `extensions/`, `skills/`, `prompts/`, or `themes/` directories
- Link to a source repository or homepage
- Are from a known maintainer or have enough metadata to review

Security note: Pi packages can run code through extensions and can instruct the agent through skills. Tell the user to review source before installing unknown third-party packages.

### 4. Present Options

For each recommendation include:

- Package name and version
- Why it matches the user's need
- Resource type if known: extension, skill, prompt, theme
- Install command
- Pi gallery and npm links
- Any security or quality caveats

Example:

```text
I found `pi-subagents` for delegating work to subagents.
Install: pi install npm:pi-subagents
Pi gallery: https://pi.dev/packages/pi-subagents
npm: https://www.npmjs.com/package/pi-subagents
```

### 5. Install Only With User Approval

If the user asks you to install, run:

```bash
pi install npm:<package-name>
```

For project-local installs:

```bash
pi install npm:<package-name> -l
```

For pinned npm versions:

```bash
pi install npm:<package-name>@<version>
```

After installing, suggest `/reload` if Pi is already running.

## When Nothing Matches

If no good package exists:

1. Say no relevant Pi package was found
2. Mention the search terms tried
3. Offer to help directly or create a custom Pi package/skill/extension

## Common Mistakes

| Mistake | Fix |
| --- | --- |
| Searching plain npm without `keywords:pi-package` | Use `scripts/search-pi-packages.mjs` first |
| Recommending unknown packages without verification | Run `npm view` and inspect metadata |
| Installing automatically | Ask for explicit user approval before install |
| Ignoring package security | Remind users Pi packages can execute code or affect agent behavior |
| Assuming pi.dev has a separate install command | Use `pi install npm:<package>` for npm packages |
