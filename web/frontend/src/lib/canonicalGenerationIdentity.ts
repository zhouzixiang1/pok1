import type { CanonicalGenerationIdentity } from "../api/control.js";

const BOT_NAME = /^national_v([1-9][0-9]*)$/;
const BOT_TAG = /^national-bot-v([1-9][0-9]*)$/;

/** Validate backend-owned identity without deriving an ordinal or tag. */
export function canonicalGenerationIdentityIssues(
  identity: CanonicalGenerationIdentity,
  expectedCanonicalVersion?: number,
): string[] {
  const issues: string[] = [];
  if (!Number.isSafeInteger(identity.generation_ordinal) || identity.generation_ordinal <= 0) {
    issues.push("generation_ordinal");
  }
  if (!Number.isSafeInteger(identity.canonical_version) || identity.canonical_version <= 0) {
    issues.push("canonical_version");
  }
  if (
    expectedCanonicalVersion !== undefined
    && identity.canonical_version !== expectedCanonicalVersion
  ) {
    issues.push("canonical_version_expected");
  }
  const nameMatch = BOT_NAME.exec(identity.canonical_bot_name);
  if (!nameMatch || Number(nameMatch[1]) !== identity.canonical_version) {
    issues.push("canonical_bot_name");
  }
  const tagMatch = BOT_TAG.exec(identity.canonical_tag);
  if (!tagMatch || Number(tagMatch[1]) !== identity.canonical_version) {
    issues.push("canonical_tag");
  }
  return [...new Set(issues)];
}

export function sameCanonicalGenerationIdentity(
  left: CanonicalGenerationIdentity,
  right: CanonicalGenerationIdentity,
): boolean {
  return left.generation_ordinal === right.generation_ordinal
    && left.canonical_version === right.canonical_version
    && left.canonical_bot_name === right.canonical_bot_name
    && left.canonical_tag === right.canonical_tag;
}

export function canonicalGenerationLabel(
  identity: CanonicalGenerationIdentity,
  expectedCanonicalVersion?: number,
): string | null {
  if (canonicalGenerationIdentityIssues(identity, expectedCanonicalVersion).length > 0) {
    return null;
  }
  return `第${identity.generation_ordinal}代 · ${identity.canonical_bot_name} · ${identity.canonical_tag}`;
}
