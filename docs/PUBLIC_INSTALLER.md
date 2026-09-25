# Public installer repository

The source repository can remain private while the browser installer is
published from [m5stack-openai-voice-installer](https://github.com/developman2013/m5stack-openai-voice-installer).

The private repository's firmware release workflow mirrors the binary and
manifest to that public repository. Configure a fine-grained GitHub token with
`Contents: Read and write` access to the installer repository as the
`INSTALLER_REPO_TOKEN` Actions secret in this repository.

The installer page is then available at:

`https://developman2013.github.io/m5stack-openai-voice-installer/`
