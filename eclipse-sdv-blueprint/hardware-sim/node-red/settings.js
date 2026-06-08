module.exports = {
  uiPort: process.env.PORT || 1880,
  flowFile: process.env.FLOW_FILE || "flows/ev-range-dashboard.json",
  userDir: __dirname,

  flowFilePretty: true,
  disableEditor: false,
  editorTheme: {
    projects: {
      enabled: false
    }
  },
  functionExternalModules: true,

  logging: {
    console: {
      level: process.env.NODE_RED_LOG_LEVEL || "info",
      metrics: false,
      audit: false
    }
  }
};
