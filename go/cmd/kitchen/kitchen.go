// Copyright (c) 2015 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

package main

import (
	"os"
	"os/signal"

	"github.com/maruel/subcommands"
	"golang.org/x/net/context"
)

type App struct {
	subcommands.DefaultApplication
	Context context.Context
}

var application = &App{
	subcommands.DefaultApplication{
		Name:  "kitchen",
		Title: "Kitchen. It can run a recipe.",
		Commands: []*subcommands.Command{
			subcommands.CmdHelp,
			cmdCook,
		},
	},
	handleInterruption(context.Background()),
}

func main() {
	os.Exit(subcommands.Run(application, os.Args[1:]))
}

func handleInterruption(ctx context.Context) context.Context {
	ctx, cancel := context.WithCancel(ctx)
	signalC := make(chan os.Signal)
	signal.Notify(signalC, os.Interrupt)
	go func() {
		interrupted := false
		for range signalC {
			if interrupted {
				os.Exit(1)
			}
			interrupted = true
			cancel()
		}
	}()
	return ctx
}
