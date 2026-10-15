# TODO

## Move screening profiles outside the repository

`profiles/` is gitignored, but old commits still track its files. Checking out such a commit
(or a branch that predates `0b3c536`) and then moving back to `main` deletes the files from disk,
because git removes tracked files when switching to a commit where they are deleted. Git also
overwrites ignored files without warning when a checkout brings in tracked versions of them.

This happened on 2026-10-15: `git pull` on `main` fast-forwarded over `0b3c536` and removed the
local profiles. They were restored from `0b3c536^`.

- Check whether `screener` can read profiles from a path outside the repo (e.g. `~/.config/screener/profiles/`
  or a configurable directory); add that if not.
- Move the personal profiles there and update README.md / doc/profiles.md.
- Until then, back up `profiles/` before checking out old commits or branches.
