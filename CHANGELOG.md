# Changelog

All notable changes to Foundry are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Foundry has been in use in our own development for almost a year. This repository
starts from the tool as it is today, as version 0.1.0. No release has been tagged from
it yet.

What changed for this public copy:

- A build starts only with an API sign-in (`ANTHROPIC_API_KEY`, or one of the cloud
  providers the Claude Agent SDK supports).
- A verify step or a review that fails to run, or a verifier that gives no report, no
  longer lets a build be called done. A passing API failure at those steps is retried,
  and a round count of 0 no longer skips them.
- A build resumed without its brief uses the brief in its session file.
- A refused model setting exits with code 2 and a message.
- The check with SkillWarden asks it to leave out the workspace's own `.git` folder,
  and still works with a SkillWarden that does not know that option.
- The model names for the API path are current.
- The README now tells how the first live build with this copy came out (November
  2026), and the roadmap carries what that build showed.
- Foundry has a sandbox of its own: `foundry sandbox build`, `run` and `proof` put a
  whole build inside bubblewrap, without root. `docs/SANDBOX.md` says what it closes
  and what it leaves open.
- A session file's folder is made when the session is first saved.
- When the planner's `claude` command fails, the error carries what it printed on its
  standard output as well as its error stream, so a failed sign-in shows its reason.
  A worker started through the `claude` command reports a failure the same way.
