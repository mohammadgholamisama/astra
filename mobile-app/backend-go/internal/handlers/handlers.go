package handlers

import (
	"context"
	"log"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gofiber/contrib/websocket"
	"github.com/gofiber/fiber/v2"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/models"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/services"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/ws"
)

const panelSyncDebounce = 2 * time.Second

type API struct {
	Agg   *services.Aggregator
	Panel *services.PanelSyncService
	Hub   *ws.Hub

	syncMu    sync.Mutex
	syncTimer *time.Timer
}

func (a *API) Health(c *fiber.Ctx) error {
	return c.JSON(models.HealthResponse{
		OK:      true,
		Service: "v2ray-ping-hub",
		Time:    time.Now().Unix(),
	})
}

func (a *API) PostPings(c *fiber.Ctx) error {
	var req models.PingBatchRequest
	if err := c.BodyParser(&req); err != nil {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "invalid json body"})
	}
	req.Operator = models.Operator(strings.ToLower(string(req.Operator)))
	if !req.Operator.Valid() {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{
			"error": "operator must be irancell | mci | rightel",
		})
	}
	if len(req.Samples) == 0 {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "samples required"})
	}
	if len(req.Samples) > 500 {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "too many samples (max 500)"})
	}

	for i := range req.Samples {
		req.Samples[i].ConfigHash = strings.TrimSpace(strings.ToLower(req.Samples[i].ConfigHash))
		if req.Samples[i].Operator == "" {
			req.Samples[i].Operator = req.Operator
		} else {
			req.Samples[i].Operator = models.Operator(strings.ToLower(string(req.Samples[i].Operator)))
		}
	}

	n, err := a.Agg.IngestBatch(c.Context(), req)
	if err != nil {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": err.Error()})
	}

	list, err := a.Agg.Ranked(c.Context())
	if err != nil {
		return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
	}

	a.Hub.BroadcastRanked(list)

	// Compare new samples with the 1h window, then reorder Astra (debounced).
	a.schedulePanelSync(panelSyncDebounce)

	return c.JSON(fiber.Map{
		"ok":       true,
		"accepted": n,
		"ranked":   list,
	})
}

func (a *API) GetRanked(c *fiber.Ctx) error {
	list, err := a.Agg.Ranked(c.Context())
	if err != nil {
		return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
	}
	return c.JSON(list)
}

type panelSyncBody struct {
	// Live page ranks from the app (already ordered). Merged with the 1h
	// Redis window so multi-device history still counts — never used to hide.
	Items []models.FingerprintRank `json:"items"`
}

func (a *API) SyncPanelNow(c *fiber.Ctx) error {
	var body panelSyncBody
	_ = c.BodyParser(&body)

	a.cancelScheduledSync()
	count, source, err := a.syncRanksToPanel(body.Items)
	if err != nil {
		return c.Status(fiber.StatusBadGateway).JSON(fiber.Map{"error": err.Error()})
	}
	if count == 0 {
		return c.JSON(fiber.Map{
			"ok":      true,
			"count":   0,
			"source":  source,
			"message": "no successful samples to apply",
		})
	}
	return c.JSON(fiber.Map{
		"ok":     true,
		"count":  count,
		"source": source,
	})
}

// RunPanelSyncLoop is a backup ticker; primary reorder runs on each /pings upload.
func (a *API) RunPanelSyncLoop(interval time.Duration) {
	if interval < time.Minute {
		interval = 10 * time.Minute
	}
	// Backup more often than the full window so stale order recovers without a new test.
	if interval > 15*time.Minute {
		interval = 15 * time.Minute
	}
	log.Printf("[panel-sync] backup every %s; primary sync on each /pings (1h compare)", interval)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for range ticker.C {
		a.syncOnce()
	}
}

func (a *API) schedulePanelSync(delay time.Duration) {
	a.syncMu.Lock()
	defer a.syncMu.Unlock()
	if a.syncTimer != nil {
		a.syncTimer.Stop()
	}
	a.syncTimer = time.AfterFunc(delay, a.syncOnce)
}

func (a *API) cancelScheduledSync() {
	a.syncMu.Lock()
	defer a.syncMu.Unlock()
	if a.syncTimer != nil {
		a.syncTimer.Stop()
		a.syncTimer = nil
	}
}

func (a *API) syncOnce() {
	n, _, err := a.syncRanksToPanel(nil)
	if err != nil {
		log.Printf("[panel-sync] %v", err)
		return
	}
	if n == 0 {
		log.Printf("[panel-sync] skip — no successful fingerprints in window")
		return
	}
	log.Printf("[panel-sync] ok — reordered %d fingerprints in Astra (1h window)", n)
}

func rankOK(r models.FingerprintRank) bool {
	pktOK := r.HasPacketLoss && r.PacketLossPct >= 0 && r.PacketLossPct < 100
	return r.AvgPing > 0 || r.DownloadKbps > 0 || r.UploadKbps > 0 || pktOK
}

func mergeRank(prev, cand models.FingerprintRank) models.FingerprintRank {
	out := prev
	if cand.AvgPing > 0 && (prev.AvgPing <= 0 || cand.AvgPing < prev.AvgPing) {
		out.AvgPing = cand.AvgPing
	}
	if cand.DownloadKbps > out.DownloadKbps {
		out.DownloadKbps = cand.DownloadKbps
	}
	if cand.UploadKbps > out.UploadKbps {
		out.UploadKbps = cand.UploadKbps
	}
	if cand.HasPacketLoss {
		if !out.HasPacketLoss || cand.PacketLossPct < out.PacketLossPct {
			out.HasPacketLoss = true
			out.PacketLossPct = cand.PacketLossPct
		}
	}
	if cand.DeviceCount > out.DeviceCount {
		out.DeviceCount = cand.DeviceCount
	}
	if cand.SampleCount > out.SampleCount {
		out.SampleCount = cand.SampleCount
	}
	if cand.Label != "" {
		out.Label = cand.Label
	}
	// Never forward failures for hide
	out.FailDeviceCount = 0
	return out
}

// syncRanksToPanel merges optional live app ranks with the 1h window, then
// reorders Astra. Successes only — never auto-hide.
func (a *API) syncRanksToPanel(live []models.FingerprintRank) (int, string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()

	window, err := a.Agg.RankedFingerprints(ctx)
	if err != nil {
		return 0, "window", err
	}

	best := map[string]models.FingerprintRank{}
	put := func(r models.FingerprintRank) {
		host := strings.TrimSpace(strings.ToLower(r.Host))
		if host == "" || r.Port < 1 || r.Port > 65535 {
			return
		}
		if !rankOK(r) {
			return
		}
		match := strings.TrimSpace(strings.ToLower(r.MatchHost))
		key := host + "|" + strconv.Itoa(r.Port) + "|" + match
		cand := r
		cand.Host = host
		cand.MatchHost = match
		cand.FailDeviceCount = 0
		if cand.DeviceCount < 1 {
			cand.DeviceCount = 1
		}
		if cand.SampleCount < 1 {
			cand.SampleCount = 1
		}
		if prev, ok := best[key]; ok {
			best[key] = mergeRank(prev, cand)
		} else {
			best[key] = cand
		}
	}

	for _, r := range window {
		put(r)
	}
	for _, r := range live {
		put(r)
	}

	source := "window"
	if len(live) > 0 {
		source = "live+window"
	}
	if len(best) == 0 {
		return 0, source, nil
	}

	filtered := make([]models.FingerprintRank, 0, len(best))
	for _, r := range best {
		filtered = append(filtered, r)
	}
	if err := a.Panel.SyncPingRanks(ctx, filtered); err != nil {
		return 0, source, err
	}
	return len(filtered), source, nil
}

// WSUpgrade gates non-websocket requests.
func WSUpgrade(c *fiber.Ctx) error {
	if websocket.IsWebSocketUpgrade(c) {
		return c.Next()
	}
	return fiber.ErrUpgradeRequired
}

func (a *API) WSRanked(c *websocket.Conn) {
	a.Hub.Register(c)
	defer a.Hub.Unregister(c)

	list, err := a.Agg.Ranked(context.Background())
	if err == nil {
		payload, _ := jsonMarshalWS(list)
		_ = c.WriteMessage(websocket.TextMessage, payload)
	}

	for {
		if _, _, err := c.ReadMessage(); err != nil {
			break
		}
	}
}

func jsonMarshalWS(list models.RankedListResponse) ([]byte, error) {
	msg := models.WSMessage{Type: "ranked", Payload: list}
	return jsonMarshal(msg)
}
