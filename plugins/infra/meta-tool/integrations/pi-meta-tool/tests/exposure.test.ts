import assert from "node:assert/strict";
import test from "node:test";
import { shouldExposeMetaTools } from "../src/exposure.ts";

const base = {
	enabled: true,
	trusted: true,
	minActiveCatalogSize: 1,
};

test("hides Meta-tools when the active catalog is empty", () => {
	assert.equal(
		shouldExposeMetaTools({ ...base, activeCatalogSize: 0 }),
		false,
	);
});

test("exposes Meta-tools once the active catalog meets the minimum", () => {
	assert.equal(
		shouldExposeMetaTools({ ...base, activeCatalogSize: 1 }),
		true,
	);
	assert.equal(
		shouldExposeMetaTools({
			...base,
			activeCatalogSize: 1,
			minActiveCatalogSize: 2,
		}),
		false,
	);
	assert.equal(
		shouldExposeMetaTools({
			...base,
			activeCatalogSize: 2,
			minActiveCatalogSize: 2,
		}),
		true,
	);
});

test("never exposes Meta-tools for disabled or untrusted projects", () => {
	assert.equal(
		shouldExposeMetaTools({ ...base, enabled: false, activeCatalogSize: 1 }),
		false,
	);
	assert.equal(
		shouldExposeMetaTools({ ...base, trusted: false, activeCatalogSize: 1 }),
		false,
	);
});
