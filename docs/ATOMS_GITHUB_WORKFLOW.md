# Atoms and GitHub deployment workflow

Verified against the official Atoms documentation on 2026-10-29.

The existing `fredericsetievi/AI-Agent-Resto-Pilot` repository remains the
development source of truth. Its branches and history do not need to be moved,
replaced, or deleted to use the Atoms GitHub connector.

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
