'use strict';
// Explicit executable override or Playwright's standard installed Chrome channel.
const path = require('node:path');
module.exports = function browserConfig() {
  return process.env.CLUBOPS_CHROME
    ? { executablePath: path.resolve(process.env.CLUBOPS_CHROME) }
    : { channel: 'chrome' };
};
