package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
)

// The two fingerprints determine different things, so they are separated:
//
// config_fp   -- includes credential VALUES, used for cache invalidation. After token rotation the tool listing may change, so the cache should go stale.
// identity_fp -- includes only credential KEY NAMES, used for approval invalidation. Token rotation must NOT force the user to re-approve every tool they already approved.
//
// Neither includes name / enabled / note: renaming a server, toggling it off and on, or editing its note must never void a cached listing or a user approval
// (this is a correction of revision 1 defect 1; regression tests are in mcp_approvals_test.go).
func fingerprint(payload any) string {
	b, _ := json.Marshal(payload)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func sortedKeys(m map[string]string) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}

// sortedPairs flattens into an ordered slice: although Go map JSON marshalling sorts by key,
// explicitly flattening makes "order-independence" visible in the code and allows both fingerprints to share the same shape.
func sortedPairs(m map[string]string) [][2]string {
	ks := sortedKeys(m)
	out := make([][2]string, 0, len(ks))
	for _, k := range ks {
		out = append(out, [2]string{k, m[k]})
	}
	return out
}

func ConfigFP(transport, url, command string, args []string, env, headers map[string]string) string {
	if args == nil {
		args = []string{}
	}
	return fingerprint([]any{
		"cfg", transport, url, command, args,
		sortedPairs(env), sortedPairs(headers),
	})
}

func IdentityFP(transport, url, command string, args []string, env, headers map[string]string) string {
	if args == nil {
		args = []string{}
	}
	return fingerprint([]any{
		"id", transport, url, command, args,
		sortedKeys(env), sortedKeys(headers),
	})
}
