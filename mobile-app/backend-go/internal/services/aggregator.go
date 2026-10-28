package services

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/models"
	"github.com/redis/go-redis/v9"
)

const (
	metaKeyPrefix = "cfg:meta:"
	// Best sample per device/operator/hash within the ranking window (default 1h)
	devPingKeyPrefix = "cfg:devping:"
	indexSetKey      = "cfg:index"
	fpIndexKey       = "cfg:fpindex"
	activeOpsKey     = "ops:active"
)

// Aggregator stores per-device pings and builds multi-device averages.
type Aggregator struct {
	rdb *redis.Client
	ttl time.Duration
}

func NewAggregator(rdb *redis.Client, ttl time.Duration) *Aggregator {
	return &Aggregator{rdb: rdb, ttl: ttl}
}

type configMeta struct {
	ConfigHash string `json:"config_hash"`
	Remark     string `json:"remark"`
	Protocol   string `json:"protocol"`
	Transport  string `json:"transport"`
	Security   string `json:"security"`
	Host       string `json:"host"`
	Port       int    `json:"port"`
	MatchHost  string `json:"match_host"`
}

type pingRecord struct {
	PingMs        int64   `json:"ping_ms"`
	DownloadKbps  float64 `json:"download_kbps,omitempty"`
	UploadKbps    float64 `json:"upload_kbps,omitempty"`
	PacketLossPct float64 `json:"packet_loss_pct,omitempty"`
	HasPacketLoss bool    `json:"has_packet_loss,omitempty"`
	UpdatedAt     int64   `json:"updated_at"`
	DeviceID      string  `json:"device_id,omitempty"`
}

func cleanHost(h string) string {
	h = strings.ToLower(strings.TrimSpace(h))
	h = strings.Trim(h, "[]")
	h = strings.TrimRight(h, ".")
	if strings.HasPrefix(h, "www.") {
		h = h[4:]
	}
	return h
}

func fingerprint(host string, port int, match string) string {
	return fmt.Sprintf("%s|%d|%s", cleanHost(host), port, cleanHost(match))
}

func recordSuccess(r pingRecord) bool {
	pktOK := r.HasPacketLoss && r.PacketLossPct >= 0 && r.PacketLossPct < 100
	return r.PingMs > 0 || r.DownloadKbps > 0 || r.UploadKbps > 0 || pktOK
}

// mergePingRecord keeps the better metrics in the ranking window.
// A worse/failed new test must not wipe a good earlier sample from the same device.
func mergePingRecord(prev, next pingRecord) pingRecord {
	out := next
	if prev.UpdatedAt == 0 && prev.PingMs == 0 && prev.DownloadKbps == 0 &&
		prev.UploadKbps == 0 && !prev.HasPacketLoss {
		return next
	}
	out.UpdatedAt = next.UpdatedAt
	if next.DeviceID != "" {
		out.DeviceID = next.DeviceID
	} else {
		out.DeviceID = prev.DeviceID
	}

	// Ping: lowest positive wins
	out.PingMs = prev.PingMs
	if next.PingMs > 0 && (prev.PingMs <= 0 || next.PingMs < prev.PingMs) {
		out.PingMs = next.PingMs
	}

	out.DownloadKbps = prev.DownloadKbps
	if next.DownloadKbps > prev.DownloadKbps {
		out.DownloadKbps = next.DownloadKbps
	}
	out.UploadKbps = prev.UploadKbps
	if next.UploadKbps > prev.UploadKbps {
		out.UploadKbps = next.UploadKbps
	}

	out.HasPacketLoss = prev.HasPacketLoss
	out.PacketLossPct = prev.PacketLossPct
	if next.HasPacketLoss {
		if !prev.HasPacketLoss || next.PacketLossPct < prev.PacketLossPct {
			out.HasPacketLoss = true
			out.PacketLossPct = next.PacketLossPct
		}
	}
	return out
}

type pendingSample struct {
	hash     string
	op       models.Operator
	dev      string
	host     string
	port     int
	match    string
	meta     configMeta
	rec      pingRecord
	pingKey  string
	devsKey  string
}

// IngestBatch stores samples and merges with any prior result in the TTL window.
func (a *Aggregator) IngestBatch(ctx context.Context, req models.PingBatchRequest) (int, error) {
	if !req.Operator.Valid() {
		return 0, fmt.Errorf("invalid operator: %s", req.Operator)
	}
	if len(req.Samples) == 0 {
		return 0, fmt.Errorf("samples is empty")
	}

	now := time.Now().UnixMilli()
	deviceID := strings.TrimSpace(req.DeviceID)
	if deviceID == "" {
		deviceID = "anonymous"
	}

	pending := make([]pendingSample, 0, len(req.Samples))
	pingKeys := make([]string, 0, len(req.Samples))

	for i := range req.Samples {
		s := &req.Samples[i]
		hash := strings.TrimSpace(strings.ToLower(s.ConfigHash))
		if hash == "" {
			continue
		}
		op := s.Operator
		if op == "" {
			op = req.Operator
		}
		if !op.Valid() {
			continue
		}
		if s.MeasuredAt <= 0 {
			s.MeasuredAt = now
		}
		dev := strings.TrimSpace(s.DeviceID)
		if dev == "" {
			dev = deviceID
		}

		host := cleanHost(s.Host)
		match := cleanHost(s.MatchHost)
		hasPacket := s.HasPacketLoss
		loss := s.PacketLossPct
		if hasPacket {
			if loss < 0 {
				loss = 0
			}
			if loss > 100 {
				loss = 100
			}
		} else {
			loss = 0
		}
		rec := pingRecord{
			PingMs:        s.PingMs,
			DownloadKbps:  s.DownloadKbps,
			UploadKbps:    s.UploadKbps,
			PacketLossPct: loss,
			HasPacketLoss: hasPacket,
			UpdatedAt:     s.MeasuredAt,
			DeviceID:      dev,
		}
		pk := devPingKey(hash, op, dev)
		pending = append(pending, pendingSample{
			hash:    hash,
			op:      op,
			dev:     dev,
			host:    host,
			port:    s.Port,
			match:   match,
			meta: configMeta{
				ConfigHash: hash,
				Remark:     strings.TrimSpace(s.Remark),
				Protocol:   strings.ToLower(strings.TrimSpace(s.Protocol)),
				Transport:  strings.ToLower(strings.TrimSpace(s.Transport)),
				Security:   strings.ToLower(strings.TrimSpace(s.Security)),
				Host:       host,
				Port:       s.Port,
				MatchHost:  match,
			},
			rec:     rec,
			pingKey: pk,
			devsKey: devIndexKey(hash, op),
		})
		pingKeys = append(pingKeys, pk)
	}

	if len(pending) == 0 {
		return 0, fmt.Errorf("no valid samples")
	}

	prevRaw, err := a.rdb.MGet(ctx, pingKeys...).Result()
	if err != nil && err != redis.Nil {
		return 0, fmt.Errorf("redis mget: %w", err)
	}

	pipe := a.rdb.Pipeline()
	accepted := 0
	for i := range pending {
		p := &pending[i]
		if i < len(prevRaw) && prevRaw[i] != nil {
			if raw, ok := prevRaw[i].(string); ok && raw != "" {
				var prev pingRecord
				if json.Unmarshal([]byte(raw), &prev) == nil {
					p.rec = mergePingRecord(prev, p.rec)
				}
			}
		}
		metaBytes, err := json.Marshal(p.meta)
		if err != nil {
			continue
		}
		recBytes, err := json.Marshal(p.rec)
		if err != nil {
			continue
		}

		pipe.Set(ctx, metaKey(p.hash), metaBytes, a.ttl)
		pipe.Set(ctx, p.pingKey, recBytes, a.ttl)
		pipe.SAdd(ctx, p.devsKey, p.dev)
		pipe.Expire(ctx, p.devsKey, a.ttl)
		pipe.SAdd(ctx, indexSetKey, p.hash)
		pipe.SAdd(ctx, activeOpsKey, string(p.op))
		pipe.Expire(ctx, indexSetKey, a.ttl*2)
		pipe.Expire(ctx, activeOpsKey, a.ttl*2)

		if p.host != "" && p.port >= 1 && p.port <= 65535 {
			fp := fingerprint(p.host, p.port, p.match)
			pipe.SAdd(ctx, fpIndexKey, fp)
			pipe.Expire(ctx, fpIndexKey, a.ttl*2)
			pipe.SAdd(ctx, fpHashesKey(fp), p.hash)
			pipe.Expire(ctx, fpHashesKey(fp), a.ttl*2)
		}
		accepted++
	}

	if accepted == 0 {
		return 0, fmt.Errorf("no valid samples")
	}
	if _, err := pipe.Exec(ctx); err != nil {
		return 0, fmt.Errorf("redis pipeline: %w", err)
	}
	return accepted, nil
}

// Ranked builds the app leaderboard (per config_hash), averaging devices.
func (a *Aggregator) Ranked(ctx context.Context) (models.RankedListResponse, error) {
	out := models.RankedListResponse{
		GeneratedAt: time.Now().UTC(),
		Configs:     []models.RankedConfig{},
	}

	hashes, err := a.rdb.SMembers(ctx, indexSetKey).Result()
	if err != nil && err != redis.Nil {
		return out, err
	}
	if len(hashes) == 0 {
		return out, nil
	}

	activeOps, err := a.activeOperators(ctx)
	if err != nil {
		return out, err
	}
	if len(activeOps) == 0 {
		activeOps = append([]models.Operator(nil), models.AllOperators...)
	}

	ranked := make([]models.RankedConfig, 0, len(hashes))
	stale := make([]string, 0)
	cutoff := time.Now().Add(-a.ttl).UnixMilli()

	for _, hash := range hashes {
		metaRaw, err := a.rdb.Get(ctx, metaKey(hash)).Bytes()
		if err == redis.Nil {
			stale = append(stale, hash)
			continue
		}
		if err != nil {
			return out, err
		}
		var meta configMeta
		if err := json.Unmarshal(metaRaw, &meta); err != nil {
			stale = append(stale, hash)
			continue
		}

		opPings := make([]models.OperatorPing, 0, len(models.AllOperators))
		success := 0
		sum := 0.0
		latest := int64(0)

		for _, op := range models.AllOperators {
			avg, updated, has := a.avgDevicePings(ctx, hash, op, cutoff)
			opPing := models.OperatorPing{
				Operator:  op,
				LabelFA:   op.LabelFA(),
				PingMs:    -1,
				UpdatedAt: updated,
				HasSample: has,
			}
			if has && avg > 0 {
				opPing.PingMs = int64(math.Round(avg))
				success++
				sum += avg
			} else if has {
				opPing.PingMs = -1
			}
			if updated > latest {
				latest = updated
			}
			opPings = append(opPings, opPing)
		}

		activeSuccess := 0
		activeSum := 0.0
		activeSet := map[models.Operator]struct{}{}
		for _, op := range activeOps {
			activeSet[op] = struct{}{}
		}
		for _, op := range opPings {
			if _, ok := activeSet[op.Operator]; !ok {
				continue
			}
			if op.HasSample && op.PingMs > 0 {
				activeSuccess++
				activeSum += float64(op.PingMs)
			}
		}

		rate := float64(activeSuccess) / float64(len(activeOps))
		if rate > 1 {
			rate = 1
		}
		avg := math.Inf(1)
		if activeSuccess > 0 {
			avg = activeSum / float64(activeSuccess)
		}

		updated := time.UnixMilli(latest).UTC()
		if latest == 0 {
			updated = out.GeneratedAt
		}

		ranked = append(ranked, models.RankedConfig{
			ConfigHash:    meta.ConfigHash,
			Remark:        meta.Remark,
			Protocol:      meta.Protocol,
			Transport:     meta.Transport,
			Security:      meta.Security,
			Host:          meta.Host,
			Port:          meta.Port,
			MatchHost:     meta.MatchHost,
			SuccessRate:   rate,
			AvgPing:       avg,
			OperatorPings: opPings,
			UpdatedAt:     updated,
			DeviceCount:   a.countDevices(ctx, hash, cutoff),
		})
	}

	if len(stale) > 0 {
		_ = a.rdb.SRem(ctx, indexSetKey, stringSliceToInterface(stale)...)
	}

	sort.SliceStable(ranked, func(i, j int) bool {
		if ranked[i].SuccessRate != ranked[j].SuccessRate {
			return ranked[i].SuccessRate > ranked[j].SuccessRate
		}
		ai, aj := ranked[i].AvgPing, ranked[j].AvgPing
		if math.IsInf(ai, 1) && math.IsInf(aj, 1) {
			return ranked[i].Remark < ranked[j].Remark
		}
		if math.IsInf(ai, 1) {
			return false
		}
		if math.IsInf(aj, 1) {
			return true
		}
		if ai != aj {
			return ai < aj
		}
		return ranked[i].Remark < ranked[j].Remark
	})

	for i := range ranked {
		ranked[i].Rank = i + 1
		if math.IsInf(ranked[i].AvgPing, 1) {
			ranked[i].AvgPing = -1
		} else {
			ranked[i].AvgPing = math.Round(ranked[i].AvgPing*10) / 10
		}
		ranked[i].SuccessRate = math.Round(ranked[i].SuccessRate*1000) / 1000
	}

	out.Configs = ranked
	out.Count = len(ranked)
	return out, nil
}

// RankedFingerprints averages all devices across hashes that share host:port:match_host.
// Used to reorder Astra config-order page.
func (a *Aggregator) RankedFingerprints(ctx context.Context) ([]models.FingerprintRank, error) {
	fps, err := a.rdb.SMembers(ctx, fpIndexKey).Result()
	if err != nil && err != redis.Nil {
		return nil, err
	}
	cutoff := time.Now().Add(-a.ttl).UnixMilli()
	out := make([]models.FingerprintRank, 0, len(fps))

	for _, fp := range fps {
		parts := strings.SplitN(fp, "|", 3)
		if len(parts) != 3 {
			continue
		}
		host := parts[0]
		var port int
		fmt.Sscanf(parts[1], "%d", &port)
		match := parts[2]
		if host == "" || port < 1 {
			continue
		}

		hashes, _ := a.rdb.SMembers(ctx, fpHashesKey(fp)).Result()
		if len(hashes) == 0 {
			continue
		}

		// deviceID → best positive ping / speed / lowest packet loss
		bestByDevice := map[string]float64{}
		bestDlByDevice := map[string]float64{}
		bestUlByDevice := map[string]float64{}
		bestLossByDevice := map[string]float64{}
		failedDevices := map[string]struct{}{}
		var label string
		var sampleN int

		for _, hash := range hashes {
			metaRaw, err := a.rdb.Get(ctx, metaKey(hash)).Bytes()
			if err == nil {
				var meta configMeta
				if json.Unmarshal(metaRaw, &meta) == nil && label == "" && meta.Remark != "" {
					label = meta.Remark
				}
			}
			for _, op := range models.AllOperators {
				devs, err := a.rdb.SMembers(ctx, devIndexKey(hash, op)).Result()
				if err != nil || len(devs) == 0 {
					continue
				}
				for _, dev := range devs {
					raw, err := a.rdb.Get(ctx, devPingKey(hash, op, dev)).Bytes()
					if err != nil {
						continue
					}
					var rec pingRecord
					if json.Unmarshal(raw, &rec) != nil {
						continue
					}
					if rec.UpdatedAt > 0 && rec.UpdatedAt < cutoff {
						continue
					}
					sampleN++
					id := rec.DeviceID
					if id == "" {
						id = dev
					}
					if !recordSuccess(rec) {
						if _, ok := bestByDevice[id]; !ok {
							failedDevices[id] = struct{}{}
						}
						continue
					}
					delete(failedDevices, id)
					if rec.PingMs > 0 {
						ms := float64(rec.PingMs)
						if prev, ok := bestByDevice[id]; !ok || ms < prev {
							bestByDevice[id] = ms
						}
					} else if _, ok := bestByDevice[id]; !ok {
						// Speed/packet-only success — count device without ping
						bestByDevice[id] = 0
					}
					if rec.DownloadKbps > 0 {
						if prev, ok := bestDlByDevice[id]; !ok || rec.DownloadKbps > prev {
							bestDlByDevice[id] = rec.DownloadKbps
						}
					}
					if rec.UploadKbps > 0 {
						if prev, ok := bestUlByDevice[id]; !ok || rec.UploadKbps > prev {
							bestUlByDevice[id] = rec.UploadKbps
						}
					}
					if rec.HasPacketLoss && rec.PacketLossPct >= 0 && rec.PacketLossPct <= 100 {
						if prev, ok := bestLossByDevice[id]; !ok || rec.PacketLossPct < prev {
							bestLossByDevice[id] = rec.PacketLossPct
						}
					}
				}
			}
		}

		avg := -1.0
		pingDevices := 0
		if len(bestByDevice) > 0 {
			var sum float64
			for _, ms := range bestByDevice {
				if ms > 0 {
					sum += ms
					pingDevices++
				}
			}
			if pingDevices > 0 {
				avg = math.Round((sum/float64(pingDevices))*10) / 10
			} else {
				avg = 0
			}
		}
		avgDl := 0.0
		if len(bestDlByDevice) > 0 {
			var sum float64
			for _, v := range bestDlByDevice {
				sum += v
			}
			avgDl = math.Round((sum/float64(len(bestDlByDevice)))*10) / 10
		}
		avgUl := 0.0
		if len(bestUlByDevice) > 0 {
			var sum float64
			for _, v := range bestUlByDevice {
				sum += v
			}
			avgUl = math.Round((sum/float64(len(bestUlByDevice)))*10) / 10
		}
		avgLoss := 0.0
		hasLoss := false
		if len(bestLossByDevice) > 0 {
			var sum float64
			for _, v := range bestLossByDevice {
				sum += v
			}
			avgLoss = math.Round((sum/float64(len(bestLossByDevice)))*10) / 10
			hasLoss = true
		}
		out = append(out, models.FingerprintRank{
			Host:            host,
			Port:            port,
			MatchHost:       match,
			Label:           label,
			AvgPing:         avg,
			DownloadKbps:    avgDl,
			UploadKbps:      avgUl,
			PacketLossPct:   avgLoss,
			HasPacketLoss:   hasLoss,
			SampleCount:     sampleN,
			DeviceCount:     len(bestByDevice),
			FailDeviceCount: len(failedDevices),
		})
	}

	return arrangeFingerprintSlots(out), nil
}

func (a *Aggregator) avgDevicePings(
	ctx context.Context,
	hash string,
	op models.Operator,
	cutoff int64,
) (avg float64, latest int64, has bool) {
	devs, err := a.rdb.SMembers(ctx, devIndexKey(hash, op)).Result()
	if err != nil || len(devs) == 0 {
		return 0, 0, false
	}
	var sum float64
	var n int
	for _, dev := range devs {
		raw, err := a.rdb.Get(ctx, devPingKey(hash, op, dev)).Bytes()
		if err != nil {
			continue
		}
		var rec pingRecord
		if json.Unmarshal(raw, &rec) != nil {
			continue
		}
		if rec.UpdatedAt > 0 && rec.UpdatedAt < cutoff {
			continue
		}
		has = true
		if rec.UpdatedAt > latest {
			latest = rec.UpdatedAt
		}
		if rec.PingMs > 0 {
			sum += float64(rec.PingMs)
			n++
		}
	}
	if n == 0 {
		return 0, latest, has
	}
	return sum / float64(n), latest, true
}

func (a *Aggregator) countDevices(ctx context.Context, hash string, cutoff int64) int {
	seen := map[string]struct{}{}
	for _, op := range models.AllOperators {
		devs, err := a.rdb.SMembers(ctx, devIndexKey(hash, op)).Result()
		if err != nil {
			continue
		}
		for _, dev := range devs {
			raw, err := a.rdb.Get(ctx, devPingKey(hash, op, dev)).Bytes()
			if err != nil {
				continue
			}
			var rec pingRecord
			if json.Unmarshal(raw, &rec) != nil {
				continue
			}
			if rec.UpdatedAt > 0 && rec.UpdatedAt < cutoff {
				continue
			}
			id := rec.DeviceID
			if id == "" {
				id = dev
			}
			seen[id] = struct{}{}
		}
	}
	return len(seen)
}

func (a *Aggregator) activeOperators(ctx context.Context) ([]models.Operator, error) {
	vals, err := a.rdb.SMembers(ctx, activeOpsKey).Result()
	if err != nil && err != redis.Nil {
		return nil, err
	}
	ops := make([]models.Operator, 0, len(vals))
	seen := map[models.Operator]struct{}{}
	for _, v := range vals {
		op := models.Operator(strings.ToLower(strings.TrimSpace(v)))
		if !op.Valid() {
			continue
		}
		if _, ok := seen[op]; ok {
			continue
		}
		seen[op] = struct{}{}
		ops = append(ops, op)
	}
	return ops, nil
}

func metaKey(hash string) string {
	return metaKeyPrefix + hash
}

func devPingKey(hash string, op models.Operator, device string) string {
	return fmt.Sprintf("%s%s:%s:%s", devPingKeyPrefix, hash, string(op), device)
}

func devIndexKey(hash string, op models.Operator) string {
	return fmt.Sprintf("cfg:devs:%s:%s", hash, string(op))
}

func fpHashesKey(fp string) string {
	return "cfg:fphashes:" + fp
}

func stringSliceToInterface(in []string) []interface{} {
	out := make([]interface{}, len(in))
	for i, v := range in {
		out[i] = v
	}
	return out
}
