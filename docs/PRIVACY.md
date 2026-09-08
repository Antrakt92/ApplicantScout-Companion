# Optional usage statistics

ApplicantScout Companion can share a few usage milestones to help us understand
whether people get through setup and use the overlay. **Sharing is off by default.**
The addon and companion work without it.

## Your choice

In Companion Settings, use **Share basic usage statistics**. The choice saves
immediately, separately from your Warcraft Logs credentials, even if setup is
incomplete. Closing setup without selecting the checkbox does not enable sharing.

Turning it off stops future reporting and retries, discards queued events, and
removes the local reporting ID and history. A request already in progress may
finish. Events already received are not deleted immediately; they expire from
the reporting database within 90 days. If you opt in again, a new ID is created.

If Windows prevents saving the choice, the app stops reporting for that session
and shows an error. The previously saved choice may return after restarting, so
resolve the save error before relying on a permanent change.

## What is sent

Each event contains only:

- A random installation ID, kept across ordinary updates.
- The companion version and reporting format version.
- The UTC date, without an exact event time.
- One of the five milestones below.

| Milestone | When it is observed |
| --- | --- |
| Sharing started | The installation first participates. |
| Version seen | A participating installation starts a version. |
| Setup completed | The app has accepted the credentials and Screenshots folder required to start. This does not prove a successful WCL lookup. |
| Addon data received | Fresh addon data containing applicants or group members is accepted. Replayed screenshots and restored caches do not count. |
| WCL result shown | The visible overlay has a real WCL metric for a fresh addon surface. Missing logs and hidden rows do not count. |

Daily activity milestones are deduplicated per installation, version and UTC
date. Reporting runs in the background with bounded retries. There is no saved
offline event queue, so network failures can leave gaps.

**Usage reports do not contain character names, realms, player identifiers,
candidate data, screenshots, credentials, folder paths or screenshot contents.**
No browsing history or keyboard activity is collected.

## Storage and interpretation

The collection service converts the random ID to a keyed hash before storing it.
It stores that hash, milestone, version and date. Its private dashboard shows
aggregate counts. The current UTC date and previous 89 dates are retained;
older events are removed from the active reporting database.

Like any Internet request, sending an event exposes network metadata such as
the connection's IP address to the hosting and network providers. The usage
service does not write IP addresses, raw installation IDs or request bodies to
its application logs. Its temporary rate limiter uses short-lived address hashes.
This is limited, pseudonymous reporting, not a claim of complete anonymity.

Active installations over 7 or 30 days means participating installations that
received fresh addon data during that period. It does not count every user:
people may decline sharing or block requests. Reinstalling, opting in again or
using another computer can create another ID. Seeing a new version does not
prove that the automatic updater installed it.

Development builds, automated tests and marked maintainer installations do not
send reports. Builds without a configured collection service also do not send.

## Other connections and local files

This option controls usage reporting only. Warcraft Logs lookups still send the
character and realm needed for the requested lookup to Warcraft Logs. Update
checks and downloads use GitHub. The companion reads the configured screenshot
folder locally; screenshots are not attached to usage or WCL requests.

See [trust and local data](../README.md#trust-and-local-data) before sharing
diagnostic files. For questions, open an [issue](https://github.com/Antrakt92/ApplicantScout-Companion/issues)
without including credentials or private player data.
