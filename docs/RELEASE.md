# Release and delivery

The Home Assistant app is published as a multi-architecture image at
`ghcr.io/developman2013/m5stack-openai-voice-gateway`.

Pull requests run the Python and firmware checks. A push to `main` publishes
the version from `home-assistant-addon/config.yaml` and the `latest` tag.
Home Assistant can then install the prebuilt image instead of building locally.

For a release, update the app version in `home-assistant-addon/config.yaml`,
merge to `main`, and verify the matching package in GitHub Container Registry.
