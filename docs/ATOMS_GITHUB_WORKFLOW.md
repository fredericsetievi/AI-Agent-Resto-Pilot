# Atoms and GitHub deployment workflow

Verified against the official Atoms documentation on 2026-10-29.

The existing `fredericsetievi/AI-Agent-Resto-Pilot` repository remains the
development source of truth. Its branches and history do not need to be moved,
replaced, or deleted to use the Atoms GitHub connector.

## Shared request convention

The root [AGENTS.md](../AGENTS.md) defines the convention for Codex sessions in
this repository. Other coding tools and human collaborators should follow this
guide explicitly if they do not load `AGENTS.md` automatically.

**Implement** means make and test the change locally. **Implement and deploy**
(or equivalent explicit wording) additionally authorizes the normal complete
release below, including the necessary release push, additive migrations and
publication. Respect narrower instructions such as review-first or no-push.
Repository instructions do not grant service access or independently authorize
unrequested deployments. Collaborators use their own authorized accounts.

## Current release sequence

1. Inspect Git status/diffs and current remote state. Preserve uncommitted work,
   branches and history. Read the current feature guide and migration files;
   finish the requested implementation and appropriate isolated tests.
2. Commit only the intended release files and push normally to the agreed
   branch in the existing source repository. If main has moved, inspect and
   reconcile normally; do not force-push or discard either contributor's work.
   Record the full reviewed source SHA. Do not assume a push publishes the app.
3. Inspect the existing Supabase schema before applying any required migration.
   For an explicitly requested deployment, apply the exact new additive SQL in
   a transaction using existing private server configuration. Verify constraints
   and RLS, and compare counts/fingerprints of affected business tables before
   and after. Applied migrations need verification, not replay. Attribute real
   incoming activity when fingerprints change; never delete it to restore a hash.
4. Synchronize the pinned release into the existing Atoms project. While the
   connector is unavailable, use the pinned public GitHub codeload archive and
   verify its SHA/archive root. Import tracked application files only:
   `service/`, `frontend/`, `landing/`, `tests/`, `docs/`, `supabase/migrations/`,
   `requirements.txt` and `pytest.ini`, under
   `app/deploy/restopilot-integration/`. Preserve the native scaffold as described
   below. Run its existing backend sync script and verify service byte equality,
   including new modules. Never upload the whole local workspace.
5. In Atoms, run the offline source suite with `FORCE_SQLITE=1` and both `TEST_PG`
   and `TEST_LOCAL_PG_DSN` unset. Exclude only `tests/test_atoms_adapter.py` there
   because it targets the GitHub export layout; report the actual test counts.
   Run relevant frontend tests plus the native frontend lint/build. Fix genuine
   failures; validate any environment-specific test adjustment in source too.
6. Restart with the native managed launcher when needed. Check the actual
   external App Viewer origin for health/readiness, signed-out401, normal owner
   login and inventory readback. Localhost success alone is insufficient. Inspect
   the UI and verify that the saved version contains the intended release.
   Read the current App Viewer origin again after a version is saved: earlier
   preview aliases can retire. V10 used a version-specific `-v10-dev.dev.atoms.dev`
   host after the earlier `-preview.app.atoms.dev` address stopped responding.
   Atoms normally saves/builds a version when the deployment task finishes;
   inspect its real status rather than repeatedly searching for a save tool.
7. Use **Update** to publish that verified version on the same domain, preserving
   badge/settings/Keys. A separate successful saved build does not mean it is
   live. Wait for publication success and verify the public application and
   relevant behavior. Keep smoke checks read-only or review/retry/cancel only;
   actual ledger approval or outbound WhatsApp tests need appropriate explicit
   authorization. Keep the Meta test callback unchanged.
8. Record the application SHA, saved/published Atoms version, migration outcome,
   actual test results, data-preservation evidence and any retained QA record.
   Update the release documentation. Documentation-only follow-up commits do
   not require another application publication.

Existing targets to verify before making changes:

- [Source repository](https://github.com/fredericsetievi/AI-Agent-Resto-Pilot)
- [Atoms project](https://atoms.dev/chat/2cc522e0e3264f43a31c2e2a62d0ba27)
- [Published app](https://restopilot-dashboard.pub.atoms.world/)
- Supabase project `dptyxkmomskjvwbrqpnx`
- Meta callback: `https://restopilot-dashboard.pub.atoms.world/api/v1/restopilot/webhooks/meta/whatsapp`

The previous release's state is recorded in
[the WhatsApp guide](WHATSAPP_INVENTORY.md). Verify live state rather than
hard-coding its version or assuming future migrations are absent.

## What the connector currently supports

[Atoms' GitHub guide](https://help.atoms.dev/en/articles/12129569-connect-github)
states that the connector creates a **new private repository in the connected
user's personal GitHub account**. It cannot attach an existing repository or
create the repository under an organization. Account/plan availability and
workspace ownership still apply.

Synchronization is manual:

- **Push Changes** sends Atoms files to the selected GitHub branch.
- **Pull Updates** brings that branch's changes into Atoms.
- For conflicting files, the source side wins. Preserve and review both sides
  before either action; never use these controls as an automatic merge.

Code synchronization and publication are separate. According to
[Atoms' publishing guide](https://help.atoms.dev/en/articles/12129577-publish-your-project),
edits stay in App Viewer until **Publish → Update**. Pushing a GitHub commit does
not itself update the published RestoPilot site.

## Recommended setup while keeping the Atoms host

If the connector can be restored, use an Atoms-created repository such as
`restopilot-atoms-deploy` as a deployment copy:

1. Connect the intended GitHub account from the existing Atoms project and let
   Atoms create the private repository. Verify the owner, private visibility,
   initial files and initial commit. Keep this project's existing domain.
2. Clone the deployment repository into a separate directory. Keep its native
   Atoms scaffold and its own Git history intact.
3. Export an allowlist of tracked application files from a reviewed commit in
   the existing development repository. Record its full source SHA in a
   deployment manifest and the normal deployment commit.
4. Commit and push the deployment update normally, then use **Pull Updates** in
   Atoms. Test the preview, save the version, and **Publish → Update**.

This avoids repeated zip uploads. It does not remove Atoms' manual pull and
publication steps. The existing pinned-commit codeload import remains a workable
fallback while the connector is unavailable.

Do not replace either `.git` directory, force-push, or merge unrelated histories.
The new repository is optional deployment infrastructure, not a reason to move
all development there. A separate hosting service with a supported GitHub build
pipeline could automate deployments from the original repository, but changing
the RestoPilot host/domain/webhook is a separate decision.

## RestoPilot packaging requirements

The exported GitHub layout and the native Atoms workspace have different source
paths. Copying `deploy/atoms/` alone into a new repository root breaks relative
paths used by the dashboard Vite plugin and backend sync script.

Preserve the working native scaffold and explicitly map application sources to
`app/deploy/restopilot-integration/{service,frontend,tests,...}` and the vendored
backend service. Keep the native frontend plugin's source path. Verify the
vendored service is byte-identical to the pinned application source, then run
the offline tests and native frontend build before publication.

New dashboard scripts must also be registered in the native plugin's `ASSETS`
map for development serving and emitted as Rollup assets in `generateBundle`,
as well as copied to the final output. Files written only in `closeBundle`
can be absent from publication's asset list even when saved preview serves them.
In particular,
`/assets/i18n.js` must be exported and loaded before `app.js`. Preserve the native
source path and port repair while applying that asset entry; do not replace the
whole native plugin with the older export. Check every HTML-referenced asset
over HTTP and exercise the ID/EN switch after saving and after publication.

The native plugin serves the marketing landing page (`landing/`, imported from
the repository) at `/` and the dashboard as a real `app.html` file next to it,
because the static host falls back to `index.html` for unknown paths. The
landing links to `/app.html`, and the Python app mirrors `/app` at `/app.html`
so the same links work locally. Verify after saving and after publication that
`/` serves the landing, `/app.html` serves the dashboard login, and
`/landing-assets/styles.css` returns the stylesheet.

Small native packaging fixes can be made directly in Atoms' **Code** editor,
then **Submit all** to save/build, followed by the same preview and public checks.
An Atoms AI prompt is not required for an edit that can be applied and verified
directly. Preserve the native file first, review its exact diff and save the
equivalent export change in GitHub; do not replace native paths or startup code.

The published V7 native workspace also contains a preview startup repair in
`app/start_app_v2.sh` and `app/frontend/vite.config.ts`: the launcher reclaims
only its own stale project processes on the canonical ports and records the
chosen ports in `app/.runtime-ports.json`; the Vite proxy reads the same file.
Keep these native files when importing application code. The older exported
`deploy/atoms/` scaffold in the development repository does not yet contain
that native repair and must not overwrite it.

Export tracked sources only. Never copy the whole local workspace. Exclude
`.env`, private Keys, history bundles, database files, receipt originals,
browser profiles, `tmp/`, logs, dependencies and generated build output. Existing
private Atoms server Keys and the existing Supabase project remain in place.

## Different account emails

Atoms' documented connection flow authorizes the GitHub account selected in its
OAuth window; it does not list a matching-email requirement. Therefore different
Atoms and GitHub email addresses should not require changing either account.
Verify the GitHub username displayed before creating the deployment repository.

CLI commit attribution is configured separately through Git's `user.email`;
use the preferred verified GitHub email or the account's GitHub `noreply` address.
See [GitHub's commit-email guide](https://docs.github.com/en/account-and-profile/how-tos/email-preferences/setting-your-commit-email-address).
The Atoms account email does not need to become the repository's commit email.

No connector/account/repository changes are implied by this document. In
particular, it does not authorize broadening OAuth access or overwriting either
repository's work.
