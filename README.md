# prbot

Watches GitHub every minute. When a PR needs you, it opens a zellij tab with Claude in your own clone of the repo and gives Claude a one-line instruction. Claude uses the repo's own `CLAUDE.md` and skills, such as `/review`. Each message tells Claude to wait for you before posting or pushing.

| Tab | When | First message to Claude | Where Claude works |
|---|---|---|---|
| `rev-N` | your review is requested | "Review PR #N. Wait for me before posting anything to GitHub." | your clone |
| `rerev-N` | your review is re-requested, or the PR's author replies to your review | "My review was re-requested on PR #N." or "The author replied to my review on PR #N: <links>.", then "Pull the PR and check whether the author's replies and any new commits resolve my review comments…" | your clone |
| `fix-N` | reviewers comment on **your** PR | "Address the reviewers' comments on my PR #N. You're in its branch's worktree…" | the PR branch's worktree: yours if one exists, otherwise a new one in `.claude/worktrees/<branch>` |
| `re-N` | you're @mentioned on someone else's PR | "I was mentioned on PR #N: <link>. Draft my reply…" | your clone |

`* rev-N` means Claude has finished its first pass and is waiting for you. `✓ rev-N` means it's about to close.

Each message also ends with: *When I say "close the tab", run …/bin/prbot done after everything else I asked for in that message (such as posting) has succeeded.*

## How PRs are found

Every poll runs four GitHub searches, filters what comes back, and turns each PR into at most one job:

```
every 60s (POLL_SECS)    "since" = last poll − 5 min (LAG_SECS), because search indexes slowly
│
├─① search  is:pr is:open review-requested:@me         no time limit: requests open right now
│     │                                                (includes requests to your teams)
│     ├─ this request (its timeline event) already handled? ────────────── skip
│     ├─ you've reviewed (or commented on) this PR before? (a re-request) ─ rerev-N
│     └─ otherwise ─────────────────────────────────────────────────────── rev-N
│
├─② search  is:pr is:open reviewed-by:@me -author:@me updated:>=since
│         + is:pr is:open commenter:@me -author:@me updated:>=since   reviews posted as plain comments
│     │  reads comments posted after your latest review (a push alone triggers nothing).
│     │  "Your latest review" is your newest GitHub review or conversation comment on the PR; its commit
│     │  comes from the review, or a comment's "Reviewed at: `sha`" line, else the PR's head at that time
│     ├─ not written by the PR's author? ───────────────────────────────── ignore
│     ├─ an inline reply in a thread you're not part of? ───────────────── ignore
│     ├─ already handled? ──────────────────────────────────────────────── ignore
│     └─ author's new conversation comment, or reply in your thread ────── rerev-N
│
├─③ search  is:pr is:open author:@me updated:>=since        your own PRs
│     │  reads the PR comments, inline code comments and reviews posted since the last poll
│     ├─ written by you, a bot, or someone in IGNORE_AUTHORS? ──────────── ignore
│     ├─ an approval with no text? ─────────────────────────────────────── ignore
│     ├─ already handled? ──────────────────────────────────────────────── ignore
│     └─ anything else new ─────────────────────────────────────────────── fix-N
│
└─④ search  is:pr is:open -author:@me mentions:@me updated:>=since
          + is:pr is:open -author:@me involves:@me updated:>=since     backstop for missed mentions
      │  reads the same new comments and reviews, plus the description of a PR opened since then
      ├─ no @mention of you (GH_USER), or only inside a "> quote"? ─────── ignore
      ├─ written by you, a bot, or someone in IGNORE_AUTHORS? ──────────── ignore
      ├─ already handled? ──────────────────────────────────────────────── ignore
      └─ new @mention ──────────────────────────────────────────────────── re-N

                 │  one job per PR per poll; if several match: rev > rerev > fix > re
                 │  everything matched is recorded as handled in state.json, so it never fires twice
                 ▼
     ┌──────────────────────── queue ───────────────────────┐
     │  a job for the same PR is already waiting? → merge   │
     └──────────────────────────┬───────────────────────────┘
                                ▼  each poll, for each waiting job:
     repo has no clone in REPO_DIRS ────────────────────────────────────── drop
     a tab for this PR is already open ─────────────────────────────────── wait (one notification)
     MAX_JOBS tabs already open ────────────────────────────────────────── wait
     check again against the PR as it is now:
        merged or closed (re-N jobs are kept anyway) ───────────────────── drop
        rev / re-requested rerev: you've reviewed since the request ────── drop
        author-reply rerev: you've reviewed or commented since ─────────── drop
        a review, but you'd reviewed this PR before ────────────────────── becomes rerev-N
                                ▼
              open a zellij tab: rev-N / rerev-N / fix-N / re-N
```

## Start / stop

Run these from your own terminal:
```bash
cd ~/claudehook
./bin/start                  # zellij session "prbot" with the poller in a tab
zellij attach prbot          # look in; switch tabs: Ctrl-t then arrows; detach: Ctrl-o d
./bin/stop                   # stop the poller (open tabs keep running)
zellij kill-session prbot    # stop everything
```

## By hand

```bash
./bin/new-job review 487
./bin/new-job rereview 487
./bin/new-job fix 490
./bin/new-job reply 'https://github.com/acme/widgets/pull/452#issuecomment-1234567890'
./bin/prbot ls               # open prbot tabs and anything queued
./bin/pr-threads 487         # a PR's reviews, code threads (open/resolved) and conversation
./bin/prbot cleanup --dry-run   # which worktrees have a merged PR and could go
./bin/prbot cleanup             # remove those worktrees and their local branches
```

## In a tab

Talk to Claude as usual. When you're happy with the draft, say "post it" or "push it".

prbot adds no permission rules of its own beyond allowing read-only commands (`gh pr view`, `gh pr diff`, `bin/pr-threads`, `prbot done`, …). Everything else, posting and pushing included, is decided by the permission mode (`PERMISSION_MODE`) and your own Claude settings. What holds a post back is the first message's "Wait for me before posting anything to GitHub", so ask to see the exact text first if you want to check it.

**Closing the tab.** The tab closes only when you say **"close the tab"**:
- "post and close the tab" posts first, then closes.
- "post it up" posts and leaves the tab open.
- "ignore it and close the tab" closes without posting.

The tab shows `✓ rev-N` and closes after 15 seconds (`CLOSE_GRACE_SECS`). Typing anything in that time keeps it open. `/exit` also closes it, and `claude --resume` in your clone brings back a closed conversation.

**New activity while a tab is open.** If something new happens on that PR, such as a push or a comment, it waits. You get one notification, and a fresh tab opens once the current one closes. Before it opens, it's checked again: if you've already reviewed the latest commit, or the PR has been merged, it's dropped. `./bin/prbot ls` shows what's waiting and why.

**`rev`, `rerev` and `re` tabs work in your actual working copy.** Nothing stops Claude from editing files or switching branches there beyond the permission mode. Your repo's `/review` reads PRs with `git fetch` and `git show`, so it doesn't need to.

**How `fix` tabs pick their folder.** Before Claude starts, prbot looks up the PR's source branch (never `main`/`master`, and not a fork):

| Branch situation | What prbot does |
|---|---|
| already has a worktree, including your main clone if it's on that branch | Claude starts there |
| exists locally, no worktree | `git worktree add .claude/worktrees/<branch>` |
| only on GitHub | fetches it and adds a worktree that tracks `origin/<branch>`, so a plain `git push` works |

If the worktree is clean and behind GitHub, it's fast-forwarded. If it has uncommitted changes, unpushed commits or has diverged, prbot leaves it alone and tells Claude in the first message.

In fix tabs Claude edits, runs checks and commits without asking. The message tells it to wait for you before pushing or posting.

## Cleanup after merge

Each poll, prbot checks the worktrees fix tabs have used. Once the PR is **merged**, it removes the worktree and the local branch, but only if:
- the branch has no commits after what was merged
- there are no uncommitted changes or untracked files
- no prbot tab is open for it, and nothing (a shell, a Claude session) is running inside it
- it isn't your main clone

If a check fails, it keeps everything and notifies you once with the reason.

Your repo squash-merges, so prbot checks that a PR merged on GitHub; git alone can't tell. It never deletes the branch on GitHub, and does nothing for a PR that was closed without merging.

`./bin/prbot cleanup` applies the same checks to **every** worktree in your clone, including ones you made yourself. Run it with `--dry-run` first.

## Config (`config.env`)

| Setting | Default | Meaning |
|---|---|---|
| `REPO_DIRS` | (empty), e.g. `acme/widgets=~/src/widgets` | your clone for each repo; repos not listed are skipped |
| `PERMISSION_MODE` | `auto` | `auto` or `manual`; the bypass modes are refused |
| `MAX_JOBS` | `3` | most tabs open at once; the rest wait |
| `CLOSE_GRACE_SECS` | `15` | delay after "close the tab"; typing cancels it |
| `POLL_SECS` / `LAG_SECS` | `60` / `300` | poll interval, and how far back each poll re-checks |
| `IGNORE_AUTHORS` | `Copilot` | logins to ignore; bot accounts are always ignored |
| `CLAUDE_EFFORT` | `medium` | effort for prbot tabs only (`claude --effort`); Claude elsewhere keeps your default |
| `CLAUDE_MODEL`, `NTFY_TOPIC` | (empty) | optional model; optional phone notifications via ntfy.sh |

## Notes

- **GitHub access:** prbot reads GitHub with your `gh` login, via search (fine-grained tokens can't use the notifications API). The token needs Contents: Read and Pull requests: Read and write.
- **First run:** pending review requests open at once. Comments and mentions count from when the poller first starts. `state.json` remembers what has been handled.
- **One tab per PR:** if a tab for that PR is already open, new activity waits until it closes.
- **Files:**
  - `prbot.log`: the log
  - `state.json`: what's been handled, and the queue
  - `worktrees.json`: worktrees fix tabs used
  - `~/.cache/prbot/claude-settings.json`: the generated tab settings (hooks and read-only allows)
