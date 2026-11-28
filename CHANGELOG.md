# Changelog

## [0.2.0](https://github.com/mhajder/glpi-mcp/compare/v0.1.1...v0.2.0) (2026-11-28)


### 🚀 Features

* **client:** make outbound User-Agent configurable ([0a33caa](https://github.com/mhajder/glpi-mcp/commit/0a33caae1732fe2181b9209b9d7b2ef8e2c91aa7))


### 🐛 Bug Fixes

* **deps:** support sentry-sdk 3 ([da43716](https://github.com/mhajder/glpi-mcp/commit/da437167fecca819e5c4603a942ecad94898e560))


### 📦 Build

* **deps:** bump dnspython from 2.9.0 to 2.9.2 ([#16](https://github.com/mhajder/glpi-mcp/issues/16)) ([2082d43](https://github.com/mhajder/glpi-mcp/commit/2082d43cd8d7889a010f4b47e4f7d9b08be0682b))
* **deps:** bump docker/login-action ([#9](https://github.com/mhajder/glpi-mcp/issues/9)) ([493bd07](https://github.com/mhajder/glpi-mcp/commit/493bd0733f67c14f1905285dc4bce312bf879de8))
* **deps:** bump opentelemetry-api from 1.45.1 to 1.45.2 ([#15](https://github.com/mhajder/glpi-mcp/issues/15)) ([f42a675](https://github.com/mhajder/glpi-mcp/commit/f42a6751963d5bcd803a1bff3bb25d4dd420e492))
* **deps:** bump the docker group across 1 directory with 2 updates ([#19](https://github.com/mhajder/glpi-mcp/issues/19)) ([38472fc](https://github.com/mhajder/glpi-mcp/commit/38472fc53389e1025709e697dba3e0d0c0fdb048))
* **deps:** bump the uv-dependencies group across 1 directory with 3 updates ([#25](https://github.com/mhajder/glpi-mcp/issues/25)) ([8b7b647](https://github.com/mhajder/glpi-mcp/commit/8b7b6475c728d5e530753bfb7f4d67934e74ad77))
* **docker:** harden runtime image and precompile bytecode ([8be8c52](https://github.com/mhajder/glpi-mcp/commit/8be8c52a9ab6009392b4aea879bff42395014544))


### 🧪 Tests

* optimize test suite runtime and scope pre-commit stages ([195b5a0](https://github.com/mhajder/glpi-mcp/commit/195b5a071a0c4a9854c15820ffb82f1d91e644c8))

## [0.1.1](https://github.com/mhajder/glpi-mcp/compare/v0.1.0...v0.1.1) (2026-11-07)


### 🐛 Bug Fixes

* read the version from the glpi-mcp-server distribution ([3ba7fed](https://github.com/mhajder/glpi-mcp/commit/3ba7fedb97455dc6476bdf2e51d2781d309e2514))


### 🧩 CI

* pin actions to commit SHAs at latest versions ([ebbfaf9](https://github.com/mhajder/glpi-mcp/commit/ebbfaf97201ea6e71e9b403f2a6ffd035dcfc598))
* release with release-please ([ae30a25](https://github.com/mhajder/glpi-mcp/commit/ae30a252c8c3728f60659d7cda25ff65a3fe3808))
* replace softprops/action-gh-release with the gh CLI ([fb72f33](https://github.com/mhajder/glpi-mcp/commit/fb72f336d6350a1b462323aec85fd3431f6c39cc))


### 📦 Build

* **deps-dev:** bump the uv-dev-dependencies group across 1 directory with 4 updates ([#8](https://github.com/mhajder/glpi-mcp/issues/8)) ([385f50b](https://github.com/mhajder/glpi-mcp/commit/385f50b2056e74b593080ea95869919d162c36e1))
* **deps:** bump the uv-dependencies group across 1 directory with 2 updates ([#5](https://github.com/mhajder/glpi-mcp/issues/5)) ([e1e0670](https://github.com/mhajder/glpi-mcp/commit/e1e06702e758c79ee927e7c2d2f43ff3f53f6938))
* **deps:** bump ty to 0.0.98 and type check for all platforms ([ab9d52d](https://github.com/mhajder/glpi-mcp/commit/ab9d52d63b4af8c80a241bee0af3ec54d86d2597))
* **deps:** raise minimum versions of dev tools and vulnerable dependencies ([6f31100](https://github.com/mhajder/glpi-mcp/commit/6f3110018f0449e52707083bd753c2465415e980))
* **deps:** stop Dependabot proposing Python pre-release and minor bumps ([cd9a348](https://github.com/mhajder/glpi-mcp/commit/cd9a3482fdfdc80bffee8c8793215d16730db4ca))
* **docker:** pin base images by version and digest and update them with Dependabot ([2756e4c](https://github.com/mhajder/glpi-mcp/commit/2756e4cc0277935a60ee5728c7592cc910dfa4f4))
* install optional extras for ty check in pre-commit hook ([914221d](https://github.com/mhajder/glpi-mcp/commit/914221d1c88937cba67d97a1ba1d38aed177b5e0))


### 🧪 Tests

* raise line coverage to 99.8% ([6bc4e32](https://github.com/mhajder/glpi-mcp/commit/6bc4e32cb35767dab66507fd6fa475958a8d4838))

## v0.1.0 (2026-10-26)

### Feat

- initialize GLPI MCP Server project
