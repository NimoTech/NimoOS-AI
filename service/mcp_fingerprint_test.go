package service

import "testing"

func base() (string, string, string, []string, map[string]string, map[string]string) {
	return "http", "https://x/mcp", "", []string{},
		map[string]string{}, map[string]string{"Authorization": "Bearer AAA"}
}

func TestIdentityFPIgnoresCredentialValues(t *testing.T) {
	tr, u, c, a, e, h := base()
	fp1 := IdentityFP(tr, u, c, a, e, h)
	h2 := map[string]string{"Authorization": "Bearer BBB"} // token rotation
	fp2 := IdentityFP(tr, u, c, a, e, h2)
	if fp1 != fp2 {
		t.Fatal("identity_fp must ignore credential VALUES; token rotation would re-ask the user")
	}
}

func TestIdentityFPTracksHeaderKeySet(t *testing.T) {
	tr, u, c, a, e, h := base()
	fp1 := IdentityFP(tr, u, c, a, e, h)
	h2 := map[string]string{"Authorization": "Bearer AAA", "X-Tenant": "t2"}
	if fp1 == IdentityFP(tr, u, c, a, e, h2) {
		t.Fatal("identity_fp must change when the header KEY SET changes")
	}
}

func TestIdentityFPTracksURL(t *testing.T) {
	tr, u, c, a, e, h := base()
	if IdentityFP(tr, u, c, a, e, h) == IdentityFP(tr, "https://other/mcp", c, a, e, h) {
		t.Fatal("identity_fp must change when the URL changes")
	}
}

func TestConfigFPTracksCredentialValues(t *testing.T) {
	tr, u, c, a, e, h := base()
	h2 := map[string]string{"Authorization": "Bearer BBB"}
	if ConfigFP(tr, u, c, a, e, h) == ConfigFP(tr, u, c, a, e, h2) {
		t.Fatal("config_fp MUST change on token rotation (it drives cache invalidation)")
	}
}

func TestFPStableAcrossMapIteration(t *testing.T) {
	tr, u, c, a, _, _ := base()
	e := map[string]string{"B": "2", "A": "1", "C": "3"}
	h := map[string]string{"Z": "z", "Y": "y"}
	want := ConfigFP(tr, u, c, a, e, h)
	for i := 0; i < 50; i++ { // Go map iteration order is random; here we magnify the exposure probability
		if ConfigFP(tr, u, c, a, e, h) != want {
			t.Fatal("fingerprint must be stable regardless of map iteration order")
		}
	}
}
