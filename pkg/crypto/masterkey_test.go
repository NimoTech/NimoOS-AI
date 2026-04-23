package crypto

import (
	"os"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestMasterKey_GenerateAndPersist(t *testing.T) {
	path := t.TempDir() + "/master.key"
	mk, err := LoadOrCreate(path)
	require.NoError(t, err)
	require.NotNil(t, mk)

	// Reload should return the same key
	mk2, err := LoadOrCreate(path)
	require.NoError(t, err)

	// Encrypt with mk, decrypt with mk2 — they must be the same key
	ciphertext, err := mk.Encrypt("test-secret")
	require.NoError(t, err)
	plaintext, err := mk2.Decrypt(ciphertext)
	require.NoError(t, err)
	require.Equal(t, "test-secret", plaintext)
}

func TestMasterKey_EncryptDecrypt(t *testing.T) {
	path := t.TempDir() + "/master.key"
	mk, _ := LoadOrCreate(path)

	plaintext := "sk-abcdefghijklmnopqrstuvwxyz123456"
	ciphertext, err := mk.Encrypt(plaintext)
	require.NoError(t, err)
	require.NotEqual(t, plaintext, ciphertext)
	require.NotEmpty(t, ciphertext)

	decrypted, err := mk.Decrypt(ciphertext)
	require.NoError(t, err)
	require.Equal(t, plaintext, decrypted)
}

func TestMasterKey_NonceIsRandom(t *testing.T) {
	path := t.TempDir() + "/master.key"
	mk, _ := LoadOrCreate(path)

	// Two encryptions of the same plaintext should produce different ciphertexts
	c1, _ := mk.Encrypt("same-text")
	c2, _ := mk.Encrypt("same-text")
	require.NotEqual(t, c1, c2, "nonce must be random per encryption")
}

func TestMasterKey_FilePermission(t *testing.T) {
	path := t.TempDir() + "/master.key"
	_, err := LoadOrCreate(path)
	require.NoError(t, err)

	info, err := os.Stat(path)
	require.NoError(t, err)
	require.Equal(t, os.FileMode(0600), info.Mode().Perm(), "key file must be owner-readable only")
}

func TestMasterKey_InvalidCiphertext(t *testing.T) {
	path := t.TempDir() + "/master.key"
	mk, _ := LoadOrCreate(path)

	_, err := mk.Decrypt("not-valid-base64!!!")
	require.Error(t, err)

	_, err = mk.Decrypt("aGVsbG8=") // valid base64 but too short for nonce
	require.Error(t, err)
}
