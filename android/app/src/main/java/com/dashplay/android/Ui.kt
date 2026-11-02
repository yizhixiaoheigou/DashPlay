package com.dashplay.android

import android.app.Activity
import android.app.UiModeManager
import android.content.Context
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.PorterDuff
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter

/** State of one status light: color, icon and words together, never color alone. */
enum class Light { OK, WAIT, BAD }

/**
 * The app's building blocks, written in code so the phone and the TV share one set of screens. Styles, colors and
 * sizes come from resources (values-television makes everything larger, values-night is the dark palette).
 */
class Ui(private val activity: Activity) {
    private val context: Context get() = activity
    val tv: Boolean = (context.getSystemService(Context.UI_MODE_SERVICE) as UiModeManager).currentModeType ==
        Configuration.UI_MODE_TYPE_TELEVISION
    private val density = context.resources.displayMetrics.density

    fun dp(value: Int): Int = (value * density).toInt()
    fun dimen(id: Int): Int = context.resources.getDimensionPixelSize(id)
    /** Resources.getColor(id, theme) is Android 6.0; the old one works everywhere and our colors take no theme. */
    @Suppress("DEPRECATION")
    fun color(id: Int): Int = context.resources.getColor(id)

    fun text(style: Int, value: CharSequence = ""): TextView = TextView(context, null, 0, style).apply { text = value }

    fun button(style: Int, label: CharSequence, onClick: () -> Unit): Button =
        Button(context, null, 0, style).apply {
            text = label
            setOnClickListener { onClick() }
        }

    fun column(): LinearLayout = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
    fun row(): LinearLayout = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }

    fun card(): LinearLayout = column().apply { setBackgroundResource(R.drawable.bg_card) }

    /** A vertical stack whose children are spaced by [gap]. */
    fun stack(gap: Int = dimen(R.dimen.gap)): LinearLayout = column().apply {
        showDividers = LinearLayout.SHOW_DIVIDER_MIDDLE
        dividerDrawable = GradientDrawable().apply { setSize(0, gap); setColor(Color.TRANSPARENT) }
    }

    fun matchWidth(): LinearLayout.LayoutParams =
        LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)

    fun weight(weight: Float = 1f): LinearLayout.LayoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, weight)

    /** The whole screen: scrolls, keeps a readable width on tablets and TVs, and centers it. */
    fun page(content: View): ScrollView {
        val gutter = dimen(R.dimen.gutter)
        val frame = FrameLayout(context).apply { setPadding(gutter, gutter, gutter, gutter) }
        frame.addView(
            content,
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER_HORIZONTAL),
        )
        val maxWidth = dimen(R.dimen.content_max)
        content.addOnLayoutChangeListener { view, _, _, _, _, _, _, _, _ ->
            val available = frame.width - 2 * gutter
            if (available > maxWidth && view.width != maxWidth) {
                view.layoutParams = (view.layoutParams as FrameLayout.LayoutParams).apply { width = maxWidth }
            }
        }
        return ScrollView(context).apply {
            isFillViewport = true
            addView(frame)
            // Android 15+ lays target-35 apps beneath system bars. Apply their actual insets to
            // the viewport so every page (including a scrolled page) stays clear of bars/cutouts.
            if (Build.VERSION.SDK_INT >= 30) {
                @Suppress("DEPRECATION")
                activity.window.statusBarColor = color(R.color.bg)
                @Suppress("DEPRECATION")
                activity.window.navigationBarColor = color(R.color.bg)
                setOnApplyWindowInsetsListener { view, insets ->
                    val safe = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
                    view.setPadding(safe.left, safe.top, safe.right, safe.bottom)
                    val light = context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK != Configuration.UI_MODE_NIGHT_YES
                    val mask = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS
                    view.windowInsetsController?.setSystemBarsAppearance(if (light) mask else 0, mask)
                    insets
                }
                addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
                    override fun onViewAttachedToWindow(view: View) = view.requestApplyInsets()
                    override fun onViewDetachedFromWindow(view: View) = Unit
                })
            }
        }
    }

    /** A round status light with its icon. */
    fun light(state: Light): FrameLayout {
        val size = dimen(R.dimen.dot)
        val tint = color(
            when (state) {
                Light.OK -> R.color.green
                Light.WAIT -> R.color.orange
                Light.BAD -> R.color.red
            },
        )
        val dot = FrameLayout(context).apply {
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(tint) }
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }
        val icon = ImageView(context).apply {
            setImageResource(
                when (state) {
                    Light.OK -> R.drawable.ic_check
                    Light.WAIT -> R.drawable.ic_clock
                    Light.BAD -> R.drawable.ic_alert
                },
            )
            setColorFilter(color(R.color.on_dot), PorterDuff.Mode.SRC_IN)
        }
        dot.addView(icon, FrameLayout.LayoutParams(size * 6 / 10, size * 6 / 10, Gravity.CENTER))
        dot.layoutParams = LinearLayout.LayoutParams(size, size)
        return dot
    }

    /** One line of the status list: light, name, and what it means now. */
    fun statusRow(name: CharSequence, value: CharSequence, state: Light): LinearLayout {
        val pad = dimen(R.dimen.pad)
        return row().apply {
            minimumHeight = dimen(R.dimen.row_height)
            setPadding(pad, pad * 2 / 3, pad, pad * 2 / 3)
            addView(light(state))
            val texts = column().apply { setPadding(pad * 3 / 4, 0, 0, 0) }
            texts.addView(text(R.style.Text_Name, name))
            texts.addView(
                text(R.style.Text_Small, value).apply {
                    setTextColor(color(if (state == Light.OK) R.color.green else if (state == Light.WAIT) R.color.orange else R.color.red))
                },
            )
            addView(texts, weight())
            contentDescription = "$name，$value"
        }
    }

    /** A card with rows separated by hairlines. */
    fun list(rows: List<View>): LinearLayout = card().apply {
        rows.forEachIndexed { index, view ->
            if (index > 0) addView(View(context).apply { setBackgroundColor(color(R.color.line)) }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 1))
            addView(view, matchWidth())
        }
    }

    /** An answer to a setup question: a title and one line saying what it leads to. */
    fun choice(title: CharSequence, sub: CharSequence, chosen: Boolean, enabled: Boolean = true, onClick: () -> Unit): LinearLayout {
        val pad = dimen(R.dimen.pad)
        return column().apply {
            setBackgroundResource(R.drawable.bg_choice)
            setPadding(pad, pad * 3 / 4, pad, pad * 3 / 4)
            minimumHeight = dimen(R.dimen.row_height)
            isFocusable = true
            isClickable = enabled
            isEnabled = enabled
            isSelected = chosen
            alpha = if (enabled) 1f else 0.55f
            addView(text(R.style.Text_Name, title).apply { setTypeface(typeface, android.graphics.Typeface.BOLD) })
            addView(text(R.style.Text_Small, sub))
            setOnClickListener { if (enabled) onClick() }
            contentDescription = "$title，$sub"
        }
    }

    /** A settings line: title, the current value under it, and a switch or an arrow. */
    fun settingRow(title: CharSequence, sub: CharSequence?, switchOn: Boolean? = null, onClick: () -> Unit): LinearLayout {
        val pad = dimen(R.dimen.pad)
        return row().apply {
            setBackgroundResource(R.drawable.bg_row)
            minimumHeight = dimen(R.dimen.row_height)
            setPadding(pad, pad * 2 / 3, pad, pad * 2 / 3)
            isFocusable = true
            isClickable = true
            val texts = column()
            texts.addView(text(R.style.Text_Name, title))
            if (!sub.isNullOrEmpty()) texts.addView(text(R.style.Text_Small, sub))
            addView(texts, weight())
            if (switchOn != null) {
                addView(
                    Switch(context).apply {
                        isChecked = switchOn
                        isClickable = false
                        isFocusable = false
                    },
                )
                contentDescription = "$title，${context.getString(if (switchOn) R.string.on else R.string.off)}"
            } else {
                addView(ImageView(context).apply { setImageResource(R.drawable.ic_chevron); importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO })
            }
            setOnClickListener { onClick() }
        }
    }

    /**
     * A labelled text field. A password field is masked by default and carries its own "show password" toggle: the Wi-Fi
     * password has to be typed on a TV remote, and a field that always shows it (or one that can never be checked)
     * both end up with the wrong password saved.
     */
    fun field(label: CharSequence, value: String, password: Boolean, fieldId: Int, revealToggle: Boolean = password): Pair<LinearLayout, EditText> {
        val box = column()
        val maskedType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        val visibleType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
        val input = EditText(context).apply {
            id = fieldId
            setText(value)
            setSingleLine()
            setTextSize(android.util.TypedValue.COMPLEX_UNIT_PX, context.resources.getDimension(R.dimen.text_body))
            setTextColor(color(R.color.text))
            setBackgroundResource(R.drawable.bg_field)
            minHeight = dimen(R.dimen.button_height)
            setPadding(dp(14), 0, dp(14), 0)
            inputType = if (password) maskedType else InputType.TYPE_CLASS_TEXT
        }
        val labelView = text(R.style.Text_Small, label).apply { setPadding(dp(4), 0, 0, dp(6)); labelFor = fieldId }
        box.addView(labelView)
        box.addView(input, matchWidth())
        if (password && revealToggle) {
            var toggle: Button? = null
            toggle = button(R.style.Button_Text, context.getString(R.string.show_password)) {
                val revealed = input.inputType == visibleType
                val cursor = input.selectionEnd.coerceAtLeast(0)
                input.inputType = if (revealed) maskedType else visibleType
                input.setSelection(cursor.coerceIn(0, input.text.length))
                toggle?.text = context.getString(if (revealed) R.string.show_password else R.string.hide_password)
                input.requestFocus()
            }
            box.addView(toggle, matchWidth())
        }
        return box to input
    }

    fun groupTitle(value: CharSequence): TextView = text(R.style.Text_Group, value)

    /** The car's address as a QR code (white quiet zone, so it scans on the dark theme too). */
    fun qr(content: String, size: Int): Bitmap {
        val matrix = QRCodeWriter().encode(content, BarcodeFormat.QR_CODE, size, size, mapOf(EncodeHintType.MARGIN to 2))
        val pixels = IntArray(size * size) { index -> if (matrix.get(index % size, index / size)) Color.BLACK else Color.WHITE }
        return Bitmap.createBitmap(pixels, size, size, Bitmap.Config.RGB_565)
    }
}
