package v2

import "github.com/NimoTech/NimoOS-AI/pkg/crypto"

// noAuthAPIKey is the credential handed to the agent / chat adapters for a
// provider that was saved without an API key — a self-hosted OpenAI-compatible
// server (llama.cpp, vLLM, OVMS, ...). An empty stored key cannot be decrypted
// (MasterKey rejects an empty ciphertext) and the agent's OpenAI SDK refuses an
// empty api_key outright, so the placeholder keeps both layers happy while the
// upstream server simply ignores the bogus bearer token.
const noAuthAPIKey = "no-key"

// decryptProviderKey returns the plaintext API key for a stored provider row,
// mapping "no key configured" to noAuthAPIKey instead of a decrypt error.
func decryptProviderKey(mk *crypto.MasterKey, enc string) (string, error) {
	if enc == "" {
		return noAuthAPIKey, nil
	}
	return mk.Decrypt(enc)
}
