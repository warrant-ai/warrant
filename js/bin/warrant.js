#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import { loadSchema, SCHEMA_VERSION, validate, ValidationError, VERSION } from "../src/index.js";

const USAGE = `usage: warrant [--version] <command>

commands:
  schema              print the decision record JSON Schema
  validate FILE...    validate one or more decision record JSON files
`;

async function validateFiles(files) {
  let failures = 0;
  for (const path of files) {
    let text;
    try {
      text = await readFile(path, "utf8");
    } catch (err) {
      if (err && err.code === "ENOENT") {
        console.error(`${path}: file not found`);
      } else {
        console.error(`${path}: cannot read file: ${err.message}`);
      }
      failures += 1;
      continue;
    }
    let record;
    try {
      record = JSON.parse(text);
    } catch (err) {
      console.error(`${path}: invalid JSON: ${err.message}`);
      failures += 1;
      continue;
    }
    try {
      validate(record);
    } catch (err) {
      if (!(err instanceof ValidationError)) throw err;
      failures += 1;
      console.error(`${path}: INVALID`);
      for (const message of err.errors) console.error(`  ${message}`);
      continue;
    }
    console.log(`${path}: valid (schema v${SCHEMA_VERSION})`);
  }
  return failures ? 1 : 0;
}

async function main(argv) {
  const [command, ...rest] = argv;
  switch (command) {
    case "--version":
    case "-v":
      console.log(`warrant ${VERSION} (schema v${SCHEMA_VERSION})`);
      return 0;
    case "schema":
      console.log(JSON.stringify(loadSchema(), null, 2));
      return 0;
    case "validate":
      if (rest.length === 0) {
        console.error("validate: at least one FILE is required");
        return 2;
      }
      return validateFiles(rest);
    case undefined:
    case "--help":
    case "-h":
      process.stdout.write(USAGE);
      return command === undefined ? 2 : 0;
    default:
      console.error(`unknown command: ${command}\n`);
      process.stderr.write(USAGE);
      return 2;
  }
}

main(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code;
  },
  (err) => {
    console.error(`warrant: ${err.message}`);
    process.exitCode = 1;
  },
);
