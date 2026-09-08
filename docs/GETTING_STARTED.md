# Get ApplicantScout working

You need the **WoW addon** and the **free Windows companion**. The addon reads
Group Finder and your group roster. The companion shows Warcraft Logs and
RaiderIO results beside WoW.

**[Download the Windows installer](https://github.com/Antrakt92/ApplicantScout-Companion/releases/latest)**

## 1. Install both parts

- Install the addon from [CurseForge](https://www.curseforge.com/wow/addons/applicantscout-lfg-overlay)
  or [Wago](https://addons.wago.io/addons/ANzke264).
- Open the companion download link above and expand **Assets** if needed.
  Choose `ApplicantScoutCompanionSetup-*.exe`, run it, and launch
  **ApplicantScout Companion** from the Start menu.

The installer is the usual choice. The portable ZIP lets you unpack and run
the program yourself; the `.sha256` file is a checksum, not an installer.
Current builds are unsigned. SmartScreen may warn or show an unknown publisher.
Download from the linked GitHub release and proceed only if you trust the
source. A checksum verifies file integrity, not publisher identity.

For a manual addon install, download `ApplicantScout-*.zip` from
[the latest addon release](https://github.com/Antrakt92/ApplicantScout-Addon/releases/latest)
and extract it so the file is at
`_retail_\Interface\AddOns\ApplicantScout\ApplicantScout.toc`.
GitHub's automatic **Source code** ZIP is not the install package.

## 2. Connect Warcraft Logs

You need a free Warcraft Logs account. ApplicantScout does not ask for your
Blizzard password.

1. Sign in to [Warcraft Logs and open API Clients](https://www.warcraftlogs.com/api/clients/).
2. Click **Create Client**.
3. Give it a name, such as `ApplicantScout`.
4. Set **Redirect URL** to exactly `http://localhost`.
5. Leave **Public Client** unchecked and create the client.
6. Copy the generated **Client ID** and **Client Secret** into the matching
   fields in ApplicantScout Companion.
7. Use **Test WCL** to check the credentials.

![Warcraft Logs Create Client form](images/wcl-create-client.jpg)

Keep the Client Secret private. These credentials let the companion request
Warcraft Logs data; they are not a WoW login. The app stores them under your
Windows user profile and uses them to authenticate with Warcraft Logs.

## 3. Choose the WoW Screenshots folder

In companion Settings, select the `Screenshots` folder inside the `_retail_`
folder for the WoW installation you play. For example:

```text
D:\Games\World of Warcraft\_retail_\Screenshots
```

This is where WoW saves screenshots taken in game. If the folder does not exist
yet, take one normal screenshot in WoW, then select the folder it creates.

Choose the WCL data you want in Settings: Mythic+, or the raid difficulties you
use. Settings save automatically. The optional RaiderIO addon provides extra
local dungeon and raid information when it is installed.

**Share basic usage statistics** is optional and unchecked. It helps us see
whether setup succeeds and the overlay gets used; it is not required to start.
See [what is shared and how to turn it off](PRIVACY.md).

## 4. Check the first results

1. Leave the companion running and reload WoW with `/reload`.
2. Make sure ApplicantScout is enabled. You can use `/apscout on`.
3. Open your Group Finder listing, or join a group and select **Party** in the
   companion.
4. When players appear, check that the companion shows the same names.

The addon briefly displays a QR code and takes a screenshot to pass group data
to the companion. The companion reads it locally; the screenshot itself is not
uploaded for a Warcraft Logs lookup. Missing logs are shown as missing, so an
empty WCL result does not necessarily mean setup failed.

Updates pause in combat, throughout an active Mythic+ run, and during raid boss
encounters. Try the first check out of combat, before starting a key.

The addon shows a short setup guide the first time you load it. After closing
it, you can reopen it at any time with `/apscout setup` to copy the companion
download link.

## If something is missing

- **No overlay:** launch the companion. Installing the WoW addon alone cannot
  display the Windows overlay. Use **Show overlay** from its tray menu.
- **No player names:** check the Screenshots path and run `/apscout status` in
  WoW. Keep ApplicantScout enabled and run `/apscout shotnow` out of combat.
- **Names appear but WCL is empty:** use **Test WCL** in Settings, check the
  selected WCL data types, and remember that a player may have no public logs.
- **Still stuck:** [report a companion issue](https://github.com/Antrakt92/ApplicantScout-Companion/issues)
  with what you expected and which step failed. Do not post credentials or
  screenshots containing private information.

Read [what the scores mean](../README.md#overlay-data) and
[which local files to redact before sharing](../README.md#trust-and-local-data).
