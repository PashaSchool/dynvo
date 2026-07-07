const { readData } = require('./io.js');
const util = require('./util');
const fs = require('fs');

function run() {
  return readData(util.tag) || fs;
}

module.exports = { run };
