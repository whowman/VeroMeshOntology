# The VeroMesh bot for Microsoft Teams

When a steward records in a meeting the facts an owner confirmed there, VeroMesh tells the
owner (docs/provenance.md, "Attesting in meetings"). By email every install can do this.
Where the customer uses Teams, the owner can get a card in their chat with the VeroMesh app
instead, with **Confirm** and **Object** for each fact. Their answer is their own
attestation or dispute, on the decision log's hash chain, as on the steward screen.

## What it depends on

Only the customer's Microsoft tenant, not where VeroMesh runs. Teams sends card presses
only to bots registered with Microsoft's Bot Framework, which is an **Azure Bot**
registration. The registration points Teams at VeroMesh's public address:
`https://<host>/api/v1/teams/messages`. VeroMesh itself can run on Azure, GCP, AWS or on
premises.

- **Azure Bot, tier F0 (free).** Teams is a standard channel, with unlimited messages.
  No web app is created: the bot runs in the VeroMesh service.
- **An Entra app registration** for the bot (this tenant only) and a client secret, which
  the install holds in a Kubernetes Secret.
- **The VeroMesh Teams app** (this folder: `manifest.json` and the icons), uploaded by a
  Teams administrator and installed for owners and stewards with an app setup policy.

No Microsoft Graph permission is needed. When the app is installed for someone, Teams tells
the bot. VeroMesh asks Teams for that person's email (or user principal name) and matches
it to one person in the model, the way sign-in does (`VEROMESH_PERSON_MATCH_FIELD`).

## Steps

1. **Register the bot** (someone who can create app registrations, with a subscription to
   hold the registration). This asks before creating anything:

   ```bash
   az login --tenant <the customer's tenant>
   HOST=ontology.example.com RESOURCE_GROUP=rg-veromesh deploy/teams/register-bot.sh
   ```

   It prints the application ID, the tenant and the client secret (shown once), and writes
   `veromesh-teams.zip`. In the portal instead: create an Azure Bot, single-tenant, F0,
   with messaging endpoint `https://<host>/api/v1/teams/messages`, and add the Microsoft
   Teams channel. Then fill `manifest.json`'s `${BOT_APP_ID}` and `${HOST}` and zip it with
   the two icons.
2. **Give the install the bot's identity** (Helm):

   ```bash
   kubectl -n <namespace> create secret generic veromesh-teams --from-literal=app-password='<client secret>'
   helm upgrade veromesh deploy/helm/veromesh --reuse-values \
     --set api.teams.appId=<application ID> --set api.teams.tenantId=<tenant ID> \
     --set api.teams.existingSecret=veromesh-teams --set api.publicUrl=https://<host>
   ```

3. **Let Microsoft reach the endpoint.** `POST /api/v1/teams/messages` must be reachable
   from the internet without a browser challenge. If a WAF or Cloudflare sits in front, it
   needs a rule that lets the path through. VeroMesh checks every request's Bot Framework
   token itself: the issuer, this bot as audience, the signature, the service URL and the
   tenant.
4. **Install the app for people** (Teams administrator). In the Teams admin center, go to
   Teams apps > Manage apps > Upload new app and choose `veromesh-teams.zip`. Then go to
   Setup policies, add VeroMesh to **Installed apps** in a policy, and assign the policy to
   the owners' and stewards' group. Teams installs the app for them, usually within a day.
   Each person gets a welcome message once VeroMesh has matched them, or a note saying it
   could not.
5. **Choose Teams** on the install's Administration > Notices page: "Teams, or email when
   Teams cannot reach them". The same page sets what notices show (the values recorded,
   with personal details never shown, or only which facts) and the reminder day (default:
   after three business days).

## Renewing

The client secret expires (a year from `register-bot.sh`). Run the script again: it adds
a new secret to the same app registration and leaves the bot as it is. Then replace the
Secret and restart the API.
