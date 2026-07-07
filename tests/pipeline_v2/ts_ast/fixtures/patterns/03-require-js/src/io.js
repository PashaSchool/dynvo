function readData(path) {
  return path;
}

function writeData(path, data) {
  return { path, data };
}

module.exports = { readData, writeData };
