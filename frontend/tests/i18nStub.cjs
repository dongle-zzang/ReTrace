// English pass-through stand-in for ~/composables/useI18n in SFC setup tests (no Nuxt runtime here).
const vue = require('vue')

const interpolate = (text, params) => params ? text.replace(/\{(\w+)\}/g, (match, name) => name in params ? String(params[name]) : match) : text
const useI18n = () => ({ locale: vue.ref('en'), dateLocale: vue.computed(() => 'en-US'), setLocale() {}, t: interpolate })

module.exports = { useI18n }
