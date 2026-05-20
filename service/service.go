package service

import (
	"database/sql"
	"os"
	"path/filepath"

	"github.com/NimoTech/NimoOS-AI/common"
	"github.com/NimoTech/NimoOS-AI/pkg/config"
	"github.com/NimoTech/NimoOS-AI/pkg/crypto"
)

// Services is the application-wide service container.
type Services interface {
	DB() *sql.DB
	MasterKey() *crypto.MasterKey
	OllamaChecker() *OllamaChecker
	LocalAdapter() *LocalAdapter
	Router() *Router
	Providers() *providerService
	ModelManager() *ModelManager
	Sessions() *sessionService
	Blacklist() *blacklistService
	Skills() *skillsService
}

type services struct {
	db            *sql.DB
	masterKey     *crypto.MasterKey
	ollamaChecker *OllamaChecker
	localAdapter  *LocalAdapter
	router        *Router
	providers     *providerService
	modelManager  *ModelManager
	sessions      *sessionService
	blacklist     *blacklistService
	skills        *skillsService
}

func (s *services) DB() *sql.DB                      { return s.db }
func (s *services) MasterKey() *crypto.MasterKey      { return s.masterKey }
func (s *services) OllamaChecker() *OllamaChecker     { return s.ollamaChecker }
func (s *services) LocalAdapter() *LocalAdapter       { return s.localAdapter }
func (s *services) Router() *Router                   { return s.router }
func (s *services) Providers() *providerService       { return s.providers }
func (s *services) ModelManager() *ModelManager       { return s.modelManager }
func (s *services) Sessions() *sessionService         { return s.sessions }
func (s *services) Blacklist() *blacklistService      { return s.blacklist }
func (s *services) Skills() *skillsService            { return s.skills }

// NewService wires all service dependencies. Panics on initialization failure.
func NewService(cfg *config.Config) Services {
	dbPath := filepath.Join(cfg.DataPath, "ai.db")
	db, err := NewDB(dbPath)
	if err != nil {
		panic("failed to open AI database: " + err.Error())
	}

	mk, err := crypto.LoadOrCreate(cfg.MasterKeyPath)
	if err != nil {
		panic("failed to load master key: " + err.Error())
	}

	providerSvc := &providerService{db: db}
	checker := NewOllamaChecker(common.OllamaBaseURL)
	local := NewLocalAdapter(common.OllamaBaseURL)
	router := &Router{providers: providerSvc, db: db}
	modelMgr := NewModelManager(common.OllamaBaseURL, db)
	sessionSvc := &sessionService{db: db}
	blacklistSvc := &blacklistService{db: db}
	skillsRoot := filepath.Join(cfg.DataPath, "skills")
	_ = os.MkdirAll(skillsRoot, 0o755)
	store := &SkillsStore{Root: skillsRoot}
	skillsSvc := &skillsService{db: db, store: store}

	// Rebuild .runtime/<uid>/ for every user we know about. The skill_state
	// table lists users; if a user has no row, the agent layer rebuilds on
	// first agent run.
	if rows, err := db.Query(`SELECT DISTINCT user_id FROM skill_state`); err == nil {
		for rows.Next() {
			var uid string
			if rows.Scan(&uid) == nil {
				uninstalled := map[string]bool{}
				disabled := map[string]bool{}
				uRows, _ := db.Query(
					`SELECT skill_id, enabled, uninstalled FROM skill_state WHERE user_id=?`, uid)
				for uRows.Next() {
					var id string
					var en, un int
					if uRows.Scan(&id, &en, &un) == nil {
						if un != 0 {
							uninstalled[id] = true
						}
						if en == 0 && un == 0 {
							disabled[id] = true
						}
					}
				}
				uRows.Close()
				_ = RebuildRuntimeView(store, uid, uninstalled, disabled)
			}
		}
		rows.Close()
	}

	return &services{
		db:            db,
		masterKey:     mk,
		ollamaChecker: checker,
		localAdapter:  local,
		router:        router,
		providers:     providerSvc,
		modelManager:  modelMgr,
		sessions:      sessionSvc,
		blacklist:     blacklistSvc,
		skills:        skillsSvc,
	}
}

// NewServiceFromParts is for tests that need to inject a SkillsStore.
func NewServiceFromParts(db *sql.DB, store *SkillsStore) Services {
	return &services{
		db:     db,
		skills: &skillsService{db: db, store: store},
	}
}
