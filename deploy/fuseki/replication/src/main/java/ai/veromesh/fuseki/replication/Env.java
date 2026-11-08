package ai.veromesh.fuseki.replication;

import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Resolves configuration strings of the form {@code env:{NAME}} or {@code env:{NAME:default}}. */
final class Env {
    private static final Pattern REF = Pattern.compile("^env:\\{([A-Za-z_][A-Za-z0-9_.]*)(?::(.*))?}$");

    static String resolve(String value) {
        return resolve(value, System.getenv());
    }

    static String resolve(String value, Map<String, String> env) {
        if (value == null)
            return null;
        Matcher m = REF.matcher(value.strip());
        if (!m.matches())
            return value;
        String v = env.get(m.group(1));
        if (v == null || v.isEmpty())
            v = System.getProperty(m.group(1));
        if (v == null || v.isEmpty())
            v = m.group(2);
        return v;
    }

    private Env() {}
}
