package crypto

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"io"
	"os"
)

// MasterKey wraps a 256-bit AES key used for encrypting sensitive fields (e.g. API keys).
// It is independent from the JWT signing key — rotating JWT certs does not affect stored data.
type MasterKey struct {
	key []byte
}

// LoadOrCreate loads an existing 32-byte key from path, or generates a new one.
// The file is created with 0600 permissions (owner-readable only).
func LoadOrCreate(path string) (*MasterKey, error) {
	data, err := os.ReadFile(path)
	if err == nil && len(data) == 32 {
		return &MasterKey{key: data}, nil
	}

	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, key, 0600); err != nil {
		return nil, err
	}
	return &MasterKey{key: key}, nil
}

// Encrypt encrypts plaintext using AES-256-GCM with a random nonce.
// Returns base64(nonce + ciphertext).
func (m *MasterKey) Encrypt(plaintext string) (string, error) {
	block, err := aes.NewCipher(m.key)
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "", err
	}
	ciphertext := gcm.Seal(nonce, nonce, []byte(plaintext), nil)
	return base64.StdEncoding.EncodeToString(ciphertext), nil
}

// Decrypt decrypts a value produced by Encrypt.
func (m *MasterKey) Decrypt(encoded string) (string, error) {
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return "", err
	}
	block, err := aes.NewCipher(m.key)
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	nonceSize := gcm.NonceSize()
	if len(data) < nonceSize {
		return "", errors.New("ciphertext too short")
	}
	plaintext, err := gcm.Open(nil, data[:nonceSize], data[nonceSize:], nil)
	if err != nil {
		return "", err
	}
	return string(plaintext), nil
}
