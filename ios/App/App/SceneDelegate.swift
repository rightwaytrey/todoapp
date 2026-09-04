import UIKit
import Capacitor
import WidgetKit

class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }

        window = UIWindow(windowScene: windowScene)
        window?.rootViewController = CAPBridgeViewController()
        window?.makeKeyAndVisible()

        SceneDelegateProxy.shared.scene(scene, willConnectTo: session, options: connectionOptions)
    }

    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        SceneDelegateProxy.shared.scene(scene, openURLContexts: URLContexts)
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        SceneDelegateProxy.shared.scene(scene, continue: userActivity)
    }

    // Refresh the home-screen widget when the user leaves the app, so a task
    // completed in here disappears from the widget straight away instead of
    // waiting out the widget's own 15-minute timeline (design.md D7).
    //
    // WHY HERE AND NOT AppDelegate: this app has a UIApplicationSceneManifest
    // (ios/App/App/Info.plist) and a UISceneDelegate, so UIKit delivers
    // lifecycle to the SCENE. The applicationDidEnterBackground /
    // applicationWillResignActive stubs the Capacitor template leaves in
    // AppDelegate.swift are never called in a scene-based app — putting the
    // reload there would look right and do nothing.
    //
    // WHY didEnterBackground AND NOT willResignActive: resign-active fires for
    // every notification banner, every Control Centre pull and every app
    // switcher peek, and each one would cost the widget a fresh http round
    // trip. Entering the background is the moment the user actually left, and
    // it is the moment before the home screen is on-screen again.
    //
    // The widget re-fetches the API itself; it does not read anything this app
    // wrote. So if a completion is still sitting in the offline queue
    // (design.md D6) the widget will still show the task, and correct itself on
    // its next refresh. Reloads asked for by a foregrounded app are not charged
    // against WidgetKit's background budget.
    func sceneDidEnterBackground(_ scene: UIScene) {
        WidgetCenter.shared.reloadAllTimelines()
    }
}
