#!/usr/bin/env ruby
# frozen_string_literal: true
#
# Adds — and now also REPAIRS — the TaskMasterWidget WidgetKit extension target
# in ios/App/App.xcodeproj. Modelled on ~/projects/prayerlist's script of the
# same name, which is the only WidgetKit target in this developer's repos that
# is known to build on the macos-26 runner.
#
# WHY A SCRIPT AND NOT A COMMITTED pbxproj: `npx cap sync ios` rewrites parts of
# the Xcode project, and this box has no Xcode to repair it with. Regenerating
# the target from a script that is idempotent means the widget survives a sync,
# a Capacitor upgrade, or a hand-edit gone wrong — re-run it and the target is
# back. It is also the only way to add a target from Linux at all.
#
# WHERE THE FILES LIVE: the project is ios/App/App.xcodeproj, so every path in a
# build setting is relative to ios/App. The sources therefore sit at
# ios/App/TaskMasterWidget/ and INFOPLIST_FILE is the plain relative
# "TaskMasterWidget/Info.plist" — no "../", which is a reliable way to lose a
# file in CI. (prayerlist's project is ios/Runner.xcodeproj, so its equivalent
# sources are at ios/PrayerWidget/. Same shape, one directory deeper here.)
#
# Run it from the repo root:
#
#   ruby scripts/add_widget_target.rb
#
# TWO MODES, ONE COMMAND. If the target is absent it is created. If it is
# already there the script does NOT bail any more: it re-applies every setting,
# link and phase membership below and prints the ones it had to change. That is
# what makes a settings change — D10 moving the widget's deployment floor to
# iOS 17, say — deliverable from Linux at all: edit the constant here, run it,
# and the existing target is brought into line.
#
# IDEMPOTENT, AND MEASURABLY SO: the project is saved ONLY when something
# actually changed, so a second run prints "nothing to do" and leaves
# project.pbxproj byte-for-byte identical. Anything that writes on every run
# would churn the pbxproj under `git diff` and hide the real change.
#
# DELIBERATELY NO ENTITLEMENTS. prayerlist sets CODE_SIGN_ENTITLEMENTS for its
# App Group; this widget has none, because an entitlement would force
# .github/workflows/testflight.yml to stop archiving unsigned (design.md D7,
# and the comment at the bottom of ios/App/App/Info.plist). The interactive
# button added in D10 does NOT need one: the App Intent POSTs to the same API
# over the tailnet, exactly as the widget's own fetch does.

require 'xcodeproj'

PROJECT_PATH = 'ios/App/App.xcodeproj'
TARGET_NAME  = 'TaskMasterWidget'
BUNDLE_ID    = 'org.rightwaytrey.taskmaster.widget'

# iOS 17, and ONLY for this target — the App target and the project-level
# setting stay at 15.0. design.md D10: the row's circle is an interactive
# widget button (`Button(intent:)`) backed by an App Intent, and both the
# button initialiser and `containerBackground` are iOS 17. An extension may
# have a HIGHER floor than its host app; the cost is that the widget does not
# appear in the gallery on a phone older than 17, and this phone is well past
# it. If this is ever lowered again, the Button(intent:) in
# TaskMasterWidget.swift has to go behind an #available check or it will not
# compile.
DEPLOY = '17.0'

# Swift auto-links what it imports, so these are belt and braces — but an
# explicit link is what makes a missing framework fail at build time with a
# readable error instead of at launch on the phone. AppIntents is D10's.
FRAMEWORKS = %w[WidgetKit SwiftUI AppIntents].freeze

# Everything the target's build configurations must say. Applied to BOTH
# configurations, in create mode and repair mode alike, so there is exactly one
# copy of each of these decisions.
SETTINGS = {
  'PRODUCT_BUNDLE_IDENTIFIER' => BUNDLE_ID,
  'PRODUCT_NAME' => TARGET_NAME,
  'INFOPLIST_FILE' => "#{TARGET_NAME}/Info.plist",

  # NO. The plist in TaskMasterWidget/Info.plist is complete and hand-written
  # (it carries the ATS exception, which no generator would invent). Letting
  # Xcode also synthesise keys on top of it is how you get a duplicate-key
  # warning and a version that disagrees with the app's.
  'GENERATE_INFOPLIST_FILE' => 'NO',

  'IPHONEOS_DEPLOYMENT_TARGET' => DEPLOY,
  'SWIFT_VERSION' => '5.0',
  'TARGETED_DEVICE_FAMILY' => '1,2',
  'SDKROOT' => 'iphoneos',

  # An appex is installed INSIDE the app, never alongside it. Without this the
  # archive tries to install the extension at the top level and export fails.
  'SKIP_INSTALL' => 'YES',

  'CODE_SIGN_STYLE' => 'Automatic',

  # The host app embeds the Swift runtime; an extension that embeds its own
  # ships a second copy and fails validation. NO is the default, said out loud
  # because it is the setting people flip when an extension will not launch.
  'ALWAYS_EMBED_SWIFT_STANDARD_LIBRARIES' => 'NO',

  # ../../Frameworks is the app bundle's Frameworks directory seen from
  # PlugIns/TaskMasterWidget.appex/ — how the appex finds anything the app
  # embedded. Copied from prayerlist, which needed it.
  'LD_RUNPATH_SEARCH_PATHS' => [
    '$(inherited)',
    '@executable_path/Frameworks',
    '@executable_path/../../Frameworks'
  ]

  # DEVELOPMENT_TEAM is deliberately absent: the workflow passes it on the
  # xcodebuild command line, which applies to every target, so hardcoding it
  # here would just be a second place to get it wrong.

  # MARKETING_VERSION / CURRENT_PROJECT_VERSION are deliberately absent too.
  # The workflow passes both on the command line so every target in the project
  # gets the same pair — an extension whose version disagrees with its host app
  # is rejected at upload. Setting them here would OVERRIDE the command line for
  # this target only, which is exactly the bug.
}.freeze

changes = []
warnings = []

unless File.directory?(PROJECT_PATH)
  abort "#{PROJECT_PATH} not found — run this from the repo root"
end

project = Xcodeproj::Project.open(PROJECT_PATH)
app = project.targets.find { |t| t.name == 'App' } or abort 'App target not found'
target = project.targets.find { |t| t.name == TARGET_NAME }

# --------------------------------------------------------------------------
# Create, if it is not there at all
# --------------------------------------------------------------------------
if target.nil?
  target = project.new_target(:app_extension, TARGET_NAME, :ios, DEPLOY)
  changes << "created the #{TARGET_NAME} app-extension target"

  # Group path is relative to the main group, which has no path of its own, so
  # it resolves against the project directory: ios/App/TaskMasterWidget.
  group = project.main_group.new_group(TARGET_NAME, TARGET_NAME)
  swift = group.new_file('TaskMasterWidget.swift')
  target.source_build_phase.add_file_reference(swift)
  changes << "added TaskMasterWidget.swift to the Sources phase"

  # A reference only — so the file is visible in Xcode's navigator. It must
  # NEVER join a build phase: an INFOPLIST_FILE that is also copied as a
  # resource ships a duplicate and fails validation.
  group.new_file('Info.plist')
end

# --------------------------------------------------------------------------
# Frameworks (create and repair)
# --------------------------------------------------------------------------
# add_system_framework finds an existing reference by its FULL path, and the
# rewrite below changes that path — so on a repair run it would happily add a
# SECOND build file for a framework that is already linked. Match on the
# basename instead, which is what "is it linked" actually means.
linked = target.frameworks_build_phase.files.map(&:file_ref).compact
               .map { |ref| File.basename(ref.path.to_s) }
FRAMEWORKS.each do |name|
  next if linked.include?("#{name}.framework")
  target.add_system_framework(name)
  changes << "linked #{name}.framework"
end

# The frameworks above — and the Foundation.framework that new_target adds by
# itself — are written by the gem as an ABSOLUTE path under DEVELOPER_DIR that
# names one SDK: ".../SDKs/iPhoneOS26.0.sdk/System/Library/Frameworks/...".
# That pins the project to whatever SDK this Linux box's gem last knew about.
# The macos-26 runner has iPhoneOS26.0 today, so it happens to work — and
# prayerlist ships exactly that, unnoticed — but the day the image moves to a
# 26.1 SDK the directory is gone and every build fails on a missing input file.
#
# Rewrite them the way Xcode itself writes a system framework: relative to
# SDKROOT, with no version in the path.
target.frameworks_build_phase.files.map(&:file_ref).compact.each do |ref|
  next unless ref.source_tree == 'DEVELOPER_DIR'
  m = ref.path.to_s.match(%r{/System/Library/Frameworks/([^/]+\.framework)\z})
  next unless m
  ref.source_tree = 'SDKROOT'
  ref.path = "System/Library/Frameworks/#{m[1]}"
  ref.name = m[1]
  changes << "re-pointed #{m[1]} at $(SDKROOT) (was pinned to one SDK version)"
end

# --------------------------------------------------------------------------
# Build settings (create and repair)
# --------------------------------------------------------------------------
target.build_configurations.each do |c|
  s = c.build_settings
  SETTINGS.each do |key, want|
    next if s[key] == want
    had = s.key?(key) ? s[key].inspect : '(unset)'
    # dup the arrays: one frozen SETTINGS entry must not become the SAME object
    # in both configurations, or a later edit to one silently edits the other.
    s[key] = want.is_a?(Array) ? want.dup : want
    changes << "#{c.name}: #{key} #{had} -> #{want.inspect}"
  end
end

# --------------------------------------------------------------------------
# Target attributes, dependency, embed (create and repair)
# --------------------------------------------------------------------------
# Xcode writes this for a target it creates itself; automatic signing at export
# time reads it. Without it, -allowProvisioningUpdates can decline to fetch a
# profile for the extension.
attributes = project.root_object.attributes['TargetAttributes'] ||= {}
want_attrs = { 'CreatedOnToolsVersion' => '26.0', 'ProvisioningStyle' => 'Automatic' }
if attributes[target.uuid] != want_attrs
  attributes[target.uuid] = want_attrs
  changes << 'set TargetAttributes (ProvisioningStyle Automatic)'
end

# Build the extension before the app that embeds it. This is also what makes
# `xcodebuild -scheme App` build the widget at all: there is no shared
# .xcscheme in this repo, so xcodebuild autocreates one per target and the App
# scheme picks up its dependencies. add_dependency is a no-op when the
# dependency is already there.
unless app.dependency_for_target(target)
  app.add_dependency(target)
  changes << 'made App depend on the widget target'
end

# dstSubfolderSpec 13 == :plug_ins == the app bundle's PlugIns directory, which
# is where iOS looks for extensions. RemoveHeadersOnCopy is what Xcode sets.
embed = app.copy_files_build_phases.find { |p| p.symbol_dst_subfolder_spec == :plug_ins }
unless embed
  embed = app.new_copy_files_build_phase('Embed Foundation Extensions')
  embed.symbol_dst_subfolder_spec = :plug_ins
  embed.dst_path = ''
  changes << "added the '#{embed.name}' copy-files phase to App"
end
unless embed.build_file(target.product_reference)
  build_file = embed.add_file_reference(target.product_reference)
  build_file.settings = { 'ATTRIBUTES' => ['RemoveHeadersOnCopy'] }
  changes << 'embedded TaskMasterWidget.appex in the App bundle (PlugIns)'
end

# Not repaired, only reported: rebuilding a lost Sources phase means guessing
# which file references were meant to be in it, and a wrong guess is worse than
# a loud line here.
sources = target.source_build_phase.files.map(&:file_ref).compact
                .map { |ref| File.basename(ref.path.to_s) }
unless sources.include?('TaskMasterWidget.swift')
  warnings << 'Sources phase does not contain TaskMasterWidget.swift — the ' \
              'appex would build empty. Delete the target in Xcode (or from ' \
              'this project file) and re-run to recreate it.'
end

# --------------------------------------------------------------------------
# Save only if something moved
# --------------------------------------------------------------------------
if changes.empty?
  puts "#{TARGET_NAME}: already correct — nothing to do"
else
  project.save
  puts "#{TARGET_NAME}: #{changes.size} change#{'s' unless changes.size == 1}"
  changes.each { |c| puts "  + #{c}" }
  puts "  bundle id  #{BUNDLE_ID}"
  puts "  deployment #{DEPLOY} (this target only; App stays at " \
       "#{app.build_configurations.first.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] || '(inherited)'})"
  puts "  sources    ios/App/#{TARGET_NAME}/TaskMasterWidget.swift"
  puts "  plist      ios/App/#{TARGET_NAME}/Info.plist  (INFOPLIST_FILE=#{TARGET_NAME}/Info.plist)"
  puts "  frameworks #{FRAMEWORKS.join(', ')} (SDKROOT-relative, not SDK-pinned)"
end

warnings.each { |w| puts "  ! #{w}" }
