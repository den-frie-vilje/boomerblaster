# Releasing Floorfill

The release procedure, including the review gate.

## 1. Review gate: three-persona README review

Before tagging, run three independent reviews of `README.md` against the
code as it stands at the release commit. Fresh reviewers each time, no
carried-over context beyond a list of what changed since the last round:

- **A user**: a competent non-programmer who wants to DJ in their office.
  Judges whether they can qualify themselves, install, set up, get a
  colleague listening, and recover, unaided; flags every unannounced
  permission prompt and every step with no visible outcome.
- **An Apple-support copywriter**: judges register (calm, second person,
  task-first, sentence-case headings, jargon gated and explained), heading
  quality, and whether detail grows top to bottom. House rules: British
  spelling, no em-dashes.
- **A sceptical developer**: verifies every claim in the README against the
  source, the installer and the Homebrew formula, path and line for each
  mismatch; walks both install routes; checks the limits section for
  completeness and honesty.

Reviewers report ranked findings and never edit. Integrate what survives
scrutiny; code findings are fixed in the same release, prose findings in the
README.

## 2. Ship

1. Bump `VERSION` in `floorfill`. If Snapweb moved, bump `SNAPWEB_VERSION`
   and `SNAPWEB_SHA256` together, from the checksum GitHub shows on the
   release's asset list, and run `floorfill init` on a real Mac to confirm
   the download and the page.
2. `python3 -m py_compile floorfill`; `bash -n install.sh uninstall.sh`;
   `shellcheck install.sh uninstall.sh`.
3. Commit with a standalone message (what was decided and why, not just
   what changed), tag `vX.Y.Z`, push branch and tag, create the GitHub
   release with upgrade notes for existing users. Confirm the Actions run
   is green.
4. `curl -sL https://github.com/den-frie-vilje/floorfill/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256`,
   then update `url` and `sha256` in
   `den-frie-vilje/homebrew-tap/Formula/floorfill.rb`; commit and push.
5. Dogfood: `brew update && brew upgrade floorfill`, `floorfill restart`,
   AirPlay something from a phone and listen on a second machine before
   considering the release done.
6. If the release changed facts the landing page states (requirements,
   install commands, the numbers), update `docs/index.html`; GitHub Pages
   redeploys on push.
