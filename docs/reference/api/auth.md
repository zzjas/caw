# Auth

Programmatic credential management for Docker containers. See the
[Docker credentials guide](../../guides/docker-credentials.md) for the full workflow and the
`caw auth` CLI.

These three functions are re-exported at the top level of `caw` (as `auth_setup`,
`auth_get_status`, `auth_get_docker_flags`) and also live in `caw.auth`.

## setup

::: caw.auth.setup

## get_status

::: caw.auth.get_status

## get_docker_flags

::: caw.auth.get_docker_flags

## teardown

::: caw.auth.teardown

## Types

::: caw.auth.AuthFileStatus
