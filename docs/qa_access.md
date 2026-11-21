# Read-only access to QA pages for reviewers

Reviewers open QA pages in a browser through CANFAR's ARC files service:

    https://ws-uv.canfar.net/arc/files/projects/<space>/<products path>/<EB>/iter_<n>/index.html

They need a CADC account, and read access to the QA folder and nothing else in the project space.
That access comes from a CADC group that holds read-only rights to the folder through a POSIX ACL.
An ACL set from a session is exactly what Storage Management's "Read" setting writes: the ARC
service reports it as the node's `groupread` property (tested 2026-11-21).

The project group (e.g. `VVINGS`) is for operators who need read/write on the whole space. The QA
group is separate, so reviewers never see `tracks/`, `inbox/` or `secrets/`.

## One-time setup (operator)

1. **Create the group.** Sign in at CADC Group Management
   (https://www.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/en/groups/) with your CADC account. Select
   **New Group**, give a **Name** (e.g. `VVINGS-QA`) and a **Description**, then **Create**. You own
   the group, so you are its administrator.
2. **Add yourself as a member, as well as administrator.** Open the group, use **Add member**, then
   **Update**. The controller and your own sessions can name the group in an ACL only if it
   resolves inside the session, and a session knows only the groups its owner belonged to when it
   started.
3. **Add the reviewers.** Same dialog: **Add member** for each reviewer's CADC username, then
   **Update**. Optionally add a second operator under **Add administrator** so membership doesn't
   depend on one person.
4. **Start a new session** (any kind). Memberships are read once, at session start. Check that
   `getent group VVINGS-QA` prints a line there.
5. **Let the group reach the QA folder.** ARC checks every folder on the way down, so the group
   needs traverse rights (`x`, which does not allow listing) on each folder above the QA folder,
   and read on the QA folder itself. In that session, with the planned path:

   ```bash
   G=VVINGS-QA
   QA=/arc/projects/VVINGS/calibrated/products
   mkdir -p "$QA"
   setfacl -m g:$G:--x /arc/projects/VVINGS /arc/projects/VVINGS/calibrated   # traverse only
   setfacl -m g:$G:rX "$QA" && setfacl -d -m g:$G:rX "$QA"                    # read + inherited
   getfacl -p /arc/projects/VVINGS "$QA" | grep "$G"
   ```

   The controller does the last line itself on every new track and iteration folder when
   `qa.group` is set, because files moved in from the track database keep their old ACLs.
   Storage Management can do the same thing from a browser: open the folder's permissions editor
   and set the group as **Read**. It does not change existing child folders, and it has no
   traverse-only setting for the parent folders, so the commands above are the reliable route.
6. **Configure the project.** In the project config:

   ```json
   "qa": {"products_dir": "/arc/projects/VVINGS/calibrated/products", "group": "VVINGS-QA"}
   ```

   Start (or restart) the controller session after step 4, so that it sees the group.

## Check it with a reviewer account (needs a second CADC account in the group)

1. While logged in at https://www.canfar.net, the reviewer opens
   `https://ws-uv.canfar.net/arc/files/projects/VVINGS/calibrated/products/index.html`. Expected:
   the page opens with no further login.
2. They open a track's QA page and an interactive plot from it.
3. They try `.../arc/files/projects/VVINGS/vla-calib/<run>/state/summary.json` (or any file outside
   the QA folder). Expected: 403.
4. An account outside the group gets 403 on the QA page too.

If step 1 asks for a login or fails, the browser isn't passing the CANFAR login to the files
service. Report that, and fall back to the external webserver (plan §5, Path A).

## Adding or removing a reviewer later

Change the group's members in Group Management. Nothing on `/arc` changes. A reviewer added while
logged in may need to log out and back in at canfar.net. Their access ends as soon as they are
removed from the group.
