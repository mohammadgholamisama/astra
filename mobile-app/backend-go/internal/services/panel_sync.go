package services

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/config"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/models"
)

// PanelSyncService pushes fingerprint rankings to Astra config-order.
type PanelSyncService struct {
	enabled bool
	baseURL string
	apiKey  string
	client  *http.Client
}

func NewPanelSyncService(cfg *config.Config) *PanelSyncService {
	return &PanelSyncService{
		enabled: cfg.PanelSyncEnabled,
		baseURL: strings.TrimRight(cfg.PanelBaseURL, "/"),
		apiKey:  cfg.PanelAPIKey,
		client: &http.Client{
			Timeout: cfg.PanelTimeout,
		},
	}
}

// SyncPingRanks reorders Astra config-order by multi-device avg ping (host/port/domain).
func (s *PanelSyncService) SyncPingRanks(ctx context.Context, ranks []models.FingerprintRank) error {
	if !s.enabled {
		return nil
	}
	if strings.TrimSpace(s.baseURL) == "" {
		return fmt.Errorf("panel base url is empty")
	}
	if strings.TrimSpace(s.apiKey) == "" {
		return fmt.Errorf("PANEL_API_KEY is empty — set it to your Astra X-Api-Key")
	}
	if len(ranks) == 0 {
		return fmt.Errorf("no fingerprint ranks to sync")
	}

	items := make([]models.FingerprintRank, 0, len(ranks))
	seen := map[string]struct{}{}
	for _, r := range ranks {
		host := strings.TrimSpace(strings.ToLower(r.Host))
		if host == "" || r.Port < 1 || r.Port > 65535 {
			continue
		}
		match := strings.TrimSpace(strings.ToLower(r.MatchHost))
		key := fmt.Sprintf("%s|%d|%s", host, r.Port, match)
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		label := strings.TrimSpace(r.Label)
		if len(label) > 128 {
			label = label[:128]
		}
		items = append(items, models.FingerprintRank{
			Host:        host,
			Port:        r.Port,
			MatchHost:   match,
			Label:       label,
			AvgPing:     r.AvgPing,
			SampleCount: r.SampleCount,
			DeviceCount: r.DeviceCount,
		})
	}
	if len(items) == 0 {
		return fmt.Errorf("no valid fingerprints")
	}

	body := models.PingRankBody{Items: items}
	payload, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}

	url := s.baseURL + "/api/admin/config-order/ping-rank"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-Api-Key", s.apiKey)

	start := time.Now()
	resp, err := s.client.Do(req)
	if err != nil {
		return fmt.Errorf("panel request failed: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("panel sync HTTP %d in %s: %s",
			resp.StatusCode, time.Since(start).Round(time.Millisecond), truncate(string(respBody), 400))
	}
	return nil
}

// SyncSortedConfigs is kept for manual sync — converts ranked configs to fingerprints.
func (s *PanelSyncService) SyncSortedConfigs(ctx context.Context, configs []models.RankedConfig) error {
	ranks := make([]models.FingerprintRank, 0, len(configs))
	for _, c := range configs {
		ranks = append(ranks, models.FingerprintRank{
			Host:      c.Host,
			Port:      c.Port,
			MatchHost: c.MatchHost,
			Label:     c.Remark,
			AvgPing:   c.AvgPing,
		})
	}
	return s.SyncPingRanks(ctx, ranks)
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
