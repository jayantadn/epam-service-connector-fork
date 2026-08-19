// Copyright (c) 2026 Eclipse Foundation.
//
// This program and the accompanying materials are made available under the
// terms of the MIT License which is available at
// https://opensource.org/licenses/MIT.
//
// SPDX-License-Identifier: MIT

module.exports = {
  uiHost: process.env.NODE_RED_HOST || "127.0.0.1",
  uiPort: process.env.PORT || 1880,
  flowFile: process.env.FLOW_FILE || "flows/ev-range-dashboard.json",
  userDir: __dirname,

  flowFilePretty: true,
  // Editor and Function-node external modules are opt-in: the editor has
  // no adminAuth configured, so leaving them on by default would let
  // anyone reaching this port edit flows and run arbitrary code.
  disableEditor: process.env.NODE_RED_ENABLE_EDITOR !== "true",
  editorTheme: {
    projects: {
      enabled: false
    }
  },
  functionExternalModules: process.env.NODE_RED_FUNCTION_EXTERNAL_MODULES === "true",

  logging: {
    console: {
      level: process.env.NODE_RED_LOG_LEVEL || "info",
      metrics: false,
      audit: false
    }
  }
};
