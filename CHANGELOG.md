# Changelog

All notable changes to Foundry are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Foundry was built inside our own software lab, where it stands at version 0.1.0. This
repository starts from the tool as it is today. Nothing has been released from it yet.

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
