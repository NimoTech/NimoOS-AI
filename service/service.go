package service

import (
	"database/sql"
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
}

func (s *services) DB() *sql.DB                  { return s.db }
func (s *services) MasterKey() *crypto.MasterKey  { return s.masterKey }
func (s *services) OllamaChecker() *OllamaChecker { return s.ollamaChecker }
func (s *services) LocalAdapter() *LocalAdapter   { return s.localAdapter }
func (s *services) Router() *Router               { return s.router }
func (s *services) Providers() *providerService   { return s.providers }
func (s *services) ModelManager() *ModelManager   { return s.modelManager }
func (s *services) Sessions() *sessionService     { return s.sessions }

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

	return &services{
		db:            db,
		masterKey:     mk,
		ollamaChecker: checker,
		localAdapter:  local,
		router:        router,
		providers:     providerSvc,
		modelManager:  modelMgr,
		sessions:      sessionSvc,
	}
}
