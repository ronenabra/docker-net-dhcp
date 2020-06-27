package plugin

import (
	"fmt"
	"io"
	"net"
	"net/http"

	docker "github.com/docker/docker/client"
	"github.com/gorilla/handlers"
	log "github.com/sirupsen/logrus"
)

// DriverName is the name of the Docker Network Driver
const DriverName string = "net-dhcp"

func writeAccessLog(w io.Writer, params handlers.LogFormatterParams) {
	log.WithFields(log.Fields{
		"status":  params.StatusCode,
		"resSize": params.Size,
	}).Debugf("%v %v", params.Request.Method, params.URL.RequestURI())
}

// Plugin is the DHCP network plugin
type Plugin struct {
	docker *docker.Client
	server http.Server
}

// NewPlugin creates a new Plugin
func NewPlugin() (*Plugin, error) {
	client, err := docker.NewClient("unix:///run/docker.sock", "v1.13.1", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create docker client: %w", err)
	}

	p := Plugin{
		docker: client,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/NetworkDriver.GetCapabilities", p.apiGetCapabilities)

	mux.HandleFunc("/NetworkDriver.CreateNetwork", p.apiCreateNetwork)
	mux.HandleFunc("/NetworkDriver.DeleteNetwork", p.apiDeleteNetwork)

	p.server = http.Server{
		Handler: handlers.CustomLoggingHandler(nil, mux, writeAccessLog),
	}

	return &p, nil
}

// Listen starts the plugin server
func (p *Plugin) Listen(bindSock string) error {
	l, err := net.Listen("unix", bindSock)
	if err != nil {
		return err
	}

	return p.server.Serve(l)
}

// Close stops the plugin server
func (p *Plugin) Close() error {
	if err := p.docker.Close(); err != nil {
		return fmt.Errorf("failed to close docker client: %w", err)
	}

	if err := p.server.Close(); err != nil {
		return fmt.Errorf("failed to close http server: %w", err)
	}

	return nil
}
