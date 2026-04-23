package service

import (
	"database/sql"
	"errors"
)

var ErrRemoteNotAllowed = errors.New("remote access is not allowed by privacy policy")

type Backend string

const (
	BackendLocal Backend = "local"
	BackendCloud Backend = "cloud"
)

type RoutingDecision struct {
	Backend    Backend
	ForceLocal bool
}

type Router struct {
	providers *providerService
	db        *sql.DB
}

// Decide returns the routing decision for a user's request.
// forceCloud corresponds to the X-NimoOS-Force-Cloud: true HTTP header.
func (r *Router) Decide(userID string, forceCloud bool) (RoutingDecision, error) {
	policy := r.getOrDefaultPolicy(userID)

	if !policy.AllowRemote {
		if forceCloud {
			return RoutingDecision{}, ErrRemoteNotAllowed
		}
		return RoutingDecision{Backend: BackendLocal, ForceLocal: true}, nil
	}

	if forceCloud || policy.DefaultBackend == "cloud" {
		return RoutingDecision{Backend: BackendCloud}, nil
	}

	return RoutingDecision{Backend: BackendLocal}, nil
}

func (r *Router) getOrDefaultPolicy(userID string) PrivacyPolicy {
	var p PrivacyPolicy
	var allowRemote, escalation int
	row := r.db.QueryRow(
		`SELECT user_id, allow_remote, default_backend, escalation_prompt FROM privacy_policies WHERE user_id=?`,
		userID,
	)
	err := row.Scan(&p.UserID, &allowRemote, &p.DefaultBackend, &escalation)
	if err != nil {
		return PrivacyPolicy{
			UserID:           userID,
			AllowRemote:      true,
			DefaultBackend:   "local",
			EscalationPrompt: true,
		}
	}
	p.AllowRemote = allowRemote == 1
	p.EscalationPrompt = escalation == 1
	return p
}
