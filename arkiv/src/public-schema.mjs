/** Purpose: define ORCA's minimal public Arkiv records.
 * Behavior: validate exact attribute sets, indexed fields and bounded retention.
 * Refs: main_implementation plan.md P11.1; api design.md §4.7. Phase: P11.
 */
import { defineSchema, MAX_STRING_BYTES, str } from '@arkiv-network/sdk/schema';
import { ExpirationTime } from '@arkiv-network/sdk';

export const publicId = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const digest = /^sha256:[0-9a-f]{64}$/;
export const entityKey = /^0x[0-9a-f]{64}$/i;
const oneOf = (...options) => value => options.includes(value);
const matches = pattern => value => pattern.test(value);
const uuid = matches(publicId);
const hashed = matches(digest);
const capability = oneOf('sst_reproduction', 'sst_review', 'temperature_summary_v1');
const basin = oneOf('global', 'arctic', 'atlantic', 'indian', 'pacific', 'southern');
const deadline = value => {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) return false;
  const seconds = Date.parse(value);
  return Number.isFinite(seconds) && new Date(seconds).toISOString() === value.replace('Z', '.000Z');
};

const profiles = {
  agent: {
    days:7, indexes:['agentId', 'capability', 'status'],
    attrs:{agentId:uuid, capability, status:oneOf('active', 'suspended', 'revoked'),
      manifestDigest:hashed, ensNameRef:matches(/^[a-z0-9-]+(?:\.[a-z0-9-]+){1,4}\.eth$/)},
  },
  task: {
    days:3, indexes:['taskId', 'capability', 'regionCode', 'status'],
    attrs:{taskId:uuid, capability, regionCode:basin, status:oneOf('open'), deadlineRef:deadline},
  },
  claim: {
    days:3, indexes:['taskId', 'agentId', 'status'],
    attrs:{taskId:uuid, agentId:uuid, status:oneOf('proposed'), bindingDigest:hashed},
  },
  assignment: {
    days:3, indexes:['taskId', 'status'],
    attrs:{taskId:uuid, claimRef:matches(entityKey), status:oneOf('selected'), assignmentDigest:hashed},
  },
  result: {
    days:28, indexes:['taskId', 'artifactDigest', 'status'],
    attrs:{taskId:uuid, artifactDigest:hashed, methodId:matches(/^[a-z0-9][a-z0-9._/-]{0,63}$/),
      status:oneOf('supported', 'insufficient', 'inconclusive')},
  },
  review: {
    days:28, indexes:['resultDigest', 'reviewerId', 'verdict'],
    attrs:{resultDigest:hashed, verdict:oneOf('agrees', 'challenges', 'inconclusive'),
      reviewerId:uuid, reviewDigest:hashed},
  },
};

for (const [kind, profile] of Object.entries(profiles)) {
  profile.schema = defineSchema(`orca.${kind}.v1`, Object.fromEntries(
    Object.keys(profile.attrs).map(name => [name, profile.indexes.includes(name) ? str().indexed() : str()]),
  ));
}

function profileFor(kind) {
  if (!Object.hasOwn(profiles, kind)) throw new Error('Unsupported ORCA public record kind');
  return profiles[kind];
}

export function publicSchema(kind) {
  return profileFor(kind).schema;
}

export function schemaInventory() {
  return Object.values(profiles).map(({schema, indexes, days}) => ({
    schema: schema.name,
    attributes: Object.keys(schema.columns),
    indexes: [...indexes],
    max_lifetime_days: days,
  }));
}

export function validatePublicAttributes(kind, attributes) {
  const {attrs} = profileFor(kind);
  if (!attributes || typeof attributes !== 'object' || Array.isArray(attributes) ||
      Object.keys(attributes).length !== Object.keys(attrs).length) {
    throw new Error('Unexpected ORCA public attributes');
  }
  return Object.fromEntries(Object.entries(attrs).map(([name, validate]) => {
    const value = Object.hasOwn(attributes, name) && attributes[name];
    if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') > MAX_STRING_BYTES || !validate(value)) {
      throw new Error(`Invalid ORCA public attribute: ${name}`);
    }
    return [name, value];
  }));
}

export function publicationOptions(kind, lifetimeSeconds) {
  const maximum = profileFor(kind).days * 86400;
  if (!Number.isSafeInteger(lifetimeSeconds) || lifetimeSeconds < 2 || lifetimeSeconds > maximum) {
    throw new Error('Public record lifetime is outside the profile bounds');
  }
  return {expiration:ExpirationTime.fromSeconds(lifetimeSeconds), flags:{readonly:true}};
}
