const { defineConfig } = require('@vue/cli-service')

module.exports = defineConfig({
  transpileDependencies: true,

  // We’re going to load files via /assets/kneader3009/ ourselves,
  // so publicPath here doesn’t matter much.
  publicPath: '',

  // So filenames are stable: app.js, chunk-vendors.js, app.css
  filenameHashing: false,

  devServer: {
    port: 8080,
  },
})
